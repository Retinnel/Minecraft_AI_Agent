package com.example.assistant;

import net.minecraft.client.Minecraft;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.core.NonNullList;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.inventory.AbstractContainerMenu;
import net.minecraft.world.inventory.ClickType;
import net.minecraft.world.inventory.CraftingMenu;
import net.minecraft.world.inventory.InventoryMenu;
import net.minecraft.world.inventory.Slot;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.crafting.Ingredient;
import net.minecraft.world.item.crafting.Recipe;
import net.minecraft.world.item.crafting.ShapedRecipe;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.Vec3;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * Крафт и установка станций — только ванильные действия клиента: клики по слотам,
 * размещение рецепта из книги рецептов и установка блока из руки. Команд нет.
 */
public final class CraftingActions {
    /** Станции, которые автопилот ищет рядом и ставит сам. */
    public static final Map<String, Block> STATIONS = Map.of(
            "crafting_table", Blocks.CRAFTING_TABLE,
            "furnace", Blocks.FURNACE);
    static final int REACH = 4;

    private CraftingActions() {
    }

    // --- общие помощники ---

    static Map<String, Integer> inventoryCounts(LocalPlayer player) {
        Map<String, Integer> counts = new HashMap<>();
        Inventory inventory = player.getInventory();
        for (List<ItemStack> part : List.of(inventory.items, inventory.offhand)) {
            for (ItemStack stack : part) {
                if (!stack.isEmpty()) {
                    counts.merge(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(),
                            stack.getCount(), Integer::sum);
                }
            }
        }
        return counts;
    }

    static int countOf(LocalPlayer player, String itemId) {
        return inventoryCounts(player).getOrDefault(itemId, 0);
    }

    static Item item(String id) {
        ResourceLocation key = ResourceLocation.tryParse(id == null ? "" : id);
        return key == null ? null : BuiltInRegistries.ITEM.getOptional(key).orElse(null);
    }

    /** Ближайший блок станции в пределах досягаемости. */
    static BlockPos nearestBlock(Minecraft mc, Block block, int radius) {
        BlockPos feet = mc.player.blockPosition();
        BlockPos best = null;
        double bestDistance = Double.MAX_VALUE;
        for (BlockPos pos : BlockPos.betweenClosed(feet.offset(-radius, -2, -radius),
                feet.offset(radius, 2, radius))) {
            if (mc.level.getBlockState(pos).is(block)) {
                double distance = pos.distSqr(feet);
                if (distance < bestDistance) {
                    bestDistance = distance;
                    best = pos.immutable();
                }
            }
        }
        return best;
    }

    /** Какие станции стоят рядом — сервер учитывает их в следующем плане. */
    static List<String> nearbyStations(Minecraft mc) {
        List<String> found = new java.util.ArrayList<>();
        if (mc.player == null || mc.level == null) {
            return found;
        }
        for (Map.Entry<String, Block> entry : STATIONS.entrySet()) {
            if (nearestBlock(mc, entry.getValue(), REACH) != null) {
                found.add(entry.getKey());
            }
        }
        return found;
    }

    /** Правый клик по блоку — открыть верстак, печь или сундук. */
    static void useBlock(Minecraft mc, BlockPos pos) {
        BlockHitResult hit = new BlockHitResult(Vec3.atCenterOf(pos), Direction.UP, pos, false);
        mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, hit);
    }

    static void click(Minecraft mc, AbstractContainerMenu menu, int slot, int button, ClickType type) {
        mc.gameMode.handleInventoryMouseClick(menu.containerId, slot, button, type, mc.player);
    }

    /** Слот инвентаря игрока в открытом меню, где лежит подходящий предмет. */
    static int playerSlotWith(AbstractContainerMenu menu, java.util.function.Predicate<ItemStack> test) {
        for (Slot slot : menu.slots) {
            if (slot.container instanceof Inventory && !slot.getItem().isEmpty() && test.test(slot.getItem())) {
                return slot.index;
            }
        }
        return -1;
    }

    /**
     * Переложить amount предметов в слот target: взять стопку, по одному правым кликом,
     * остаток вернуть. Возвращает, сколько переложено.
     */
    static int moveInto(Minecraft mc, AbstractContainerMenu menu, java.util.function.Predicate<ItemStack> test,
                        int target, int amount) {
        int moved = 0;
        int guard = 0;
        while (moved < amount && guard++ < 8) {
            int source = playerSlotWith(menu, test);
            if (source < 0) {
                break;
            }
            click(mc, menu, source, 0, ClickType.PICKUP);
            while (moved < amount && !menu.getCarried().isEmpty()) {
                click(mc, menu, target, 1, ClickType.PICKUP);
                moved++;
            }
            if (!menu.getCarried().isEmpty()) {
                click(mc, menu, source, 0, ClickType.PICKUP);
            }
        }
        return moved;
    }

    // --- крафт ---

    static AutopilotTask.Execution craft(Minecraft mc, AutopilotTask task) {
        return new CraftExecution(task);
    }

    private static final class CraftExecution implements AutopilotTask.Execution {
        private enum Phase { OPEN, WAIT_OPEN, PLACE, WAIT_RESULT, MANUAL, WAIT_MANUAL, TAKE }

        private final AutopilotTask task;
        private Phase phase = Phase.OPEN;
        private int wait;
        private int runsDone;
        private boolean openedTable;
        private Recipe<?> recipe;

        CraftExecution(AutopilotTask task) {
            this.task = task;
        }

        @Override
        public AutopilotTask.Result tick(Minecraft mc) {
            LocalPlayer player = mc.player;
            switch (phase) {
                case OPEN -> {
                    ResourceLocation key = ResourceLocation.tryParse(task.recipe_id == null ? "" : task.recipe_id);
                    Optional<? extends Recipe<?>> found = key == null ? Optional.empty()
                            : mc.level.getRecipeManager().byKey(key);
                    if (found.isEmpty()) {
                        return AutopilotTask.Result.failed("unsupported");
                    }
                    recipe = found.get();
                    if ("crafting_table".equals(task.station)) {
                        BlockPos table = nearestBlock(mc, Blocks.CRAFTING_TABLE, REACH);
                        if (table == null) {
                            return AutopilotTask.Result.failed("not_found");
                        }
                        useBlock(mc, table);
                        openedTable = true;
                        phase = Phase.WAIT_OPEN;
                        wait = 40;
                    } else {
                        if (player.containerMenu != player.inventoryMenu) {
                            player.closeContainer();
                        }
                        phase = Phase.PLACE;
                    }
                }
                case WAIT_OPEN -> {
                    if (player.containerMenu instanceof CraftingMenu) {
                        phase = Phase.PLACE;
                    } else if (--wait <= 0) {
                        return AutopilotTask.Result.failed("blocked");
                    }
                }
                case PLACE -> {
                    AbstractContainerMenu menu = menu(player);
                    if (menu == null) {
                        return AutopilotTask.Result.interrupted(SafetyMonitor.PLAYER_INPUT);
                    }
                    // Рецепт кладётся так же, как щелчком в книге рецептов.
                    mc.gameMode.handlePlaceRecipe(menu.containerId, recipe, false);
                    phase = Phase.WAIT_RESULT;
                    wait = 6;
                }
                case WAIT_RESULT, WAIT_MANUAL -> {
                    AbstractContainerMenu menu = menu(player);
                    if (menu == null) {
                        return AutopilotTask.Result.interrupted(SafetyMonitor.PLAYER_INPUT);
                    }
                    if (!menu.getSlot(0).getItem().isEmpty()) {
                        phase = Phase.TAKE;
                    } else if (--wait <= 0) {
                        if (phase == Phase.WAIT_MANUAL) {
                            return AutopilotTask.Result.failed("blocked");
                        }
                        // Книга рецептов не сработала — раскладка кликами по форме рецепта.
                        phase = Phase.MANUAL;
                    }
                }
                case MANUAL -> {
                    AbstractContainerMenu menu = menu(player);
                    if (menu == null) {
                        return AutopilotTask.Result.interrupted(SafetyMonitor.PLAYER_INPUT);
                    }
                    if (!layOut(mc, menu, recipe)) {
                        return AutopilotTask.Result.failed("blocked");
                    }
                    phase = Phase.WAIT_MANUAL;
                    wait = 6;
                }
                case TAKE -> {
                    AbstractContainerMenu menu = menu(player);
                    if (menu == null) {
                        return AutopilotTask.Result.interrupted(SafetyMonitor.PLAYER_INPUT);
                    }
                    click(mc, menu, 0, 0, ClickType.QUICK_MOVE);
                    runsDone++;
                    if (runsDone >= Math.max(1, task.runs)) {
                        close(mc);
                        return AutopilotTask.Result.done();
                    }
                    phase = Phase.PLACE;
                }
            }
            return null;
        }

        private AbstractContainerMenu menu(LocalPlayer player) {
            AbstractContainerMenu menu = player.containerMenu;
            if (openedTable) {
                return menu instanceof CraftingMenu ? menu : null;
            }
            return menu instanceof InventoryMenu ? menu : player.inventoryMenu;
        }

        private void close(Minecraft mc) {
            if (openedTable && mc.player.containerMenu instanceof CraftingMenu) {
                mc.player.closeContainer();
            }
        }

        @Override
        public void cancel(Minecraft mc) {
            close(mc);
        }
    }

    /** Раскладка одного прогона рецепта кликами: для формы — по местам, иначе подряд. */
    static boolean layOut(Minecraft mc, AbstractContainerMenu menu, Recipe<?> recipe) {
        int gridWidth = menu instanceof CraftingMenu ? 3 : 2;
        NonNullList<Ingredient> ingredients = recipe.getIngredients();
        int width = recipe instanceof ShapedRecipe shaped ? shaped.getWidth() : gridWidth;
        if (width > gridWidth) {
            return false;
        }
        for (int i = 0; i < ingredients.size(); i++) {
            Ingredient ingredient = ingredients.get(i);
            if (ingredient.isEmpty()) {
                continue;
            }
            int row = i / width;
            int column = i % width;
            if (row >= gridWidth) {
                return false;
            }
            int target = 1 + row * gridWidth + column;
            if (moveInto(mc, menu, ingredient, target, 1) < 1) {
                return false;
            }
        }
        return true;
    }

    // --- установка станции ---

    static AutopilotTask.Execution placeStation(Minecraft mc, AutopilotTask task) {
        return new PlaceExecution(task);
    }

    private static final class PlaceExecution implements AutopilotTask.Execution {
        private final AutopilotTask task;
        private BlockPos target;
        private int wait;

        PlaceExecution(AutopilotTask task) {
            this.task = task;
        }

        @Override
        public AutopilotTask.Result tick(Minecraft mc) {
            Block block = STATIONS.get(task.station);
            Item item = item(task.item);
            if (block == null || item == null) {
                return AutopilotTask.Result.failed("unsupported");
            }
            if (target != null) {
                if (mc.level.getBlockState(target).is(block)) {
                    return AutopilotTask.Result.done();
                }
                return --wait <= 0 ? AutopilotTask.Result.failed("blocked") : null;
            }
            if (nearestBlock(mc, block, REACH) != null) {
                return AutopilotTask.Result.done();
            }
            if (!toHand(mc, item)) {
                return AutopilotTask.Result.failed("no_tool");
            }
            BlockPos spot = freeSpot(mc);
            if (spot == null) {
                return AutopilotTask.Result.failed("blocked");
            }
            BlockPos below = spot.below();
            BlockHitResult hit = new BlockHitResult(Vec3.atCenterOf(below).add(0, 0.5, 0), Direction.UP,
                    below, false);
            mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, hit);
            target = spot;
            wait = 10;
            return null;
        }
    }

    /** Взять предмет в руку: из панели быстрого доступа выбором, из рюкзака — обменом слотов. */
    static boolean toHand(Minecraft mc, Item item) {
        Inventory inventory = mc.player.getInventory();
        for (int i = 0; i < 9; i++) {
            if (inventory.items.get(i).is(item)) {
                inventory.selected = i;
                return true;
            }
        }
        for (int i = 9; i < inventory.items.size(); i++) {
            if (inventory.items.get(i).is(item)) {
                // В меню инвентаря слоты рюкзака 9–35 совпадают с номерами в Inventory.
                click(mc, mc.player.inventoryMenu, i, inventory.selected, ClickType.SWAP);
                return true;
            }
        }
        return false;
    }

    /** Свободное место рядом с игроком на твёрдом блоке, не там, где стоит он сам. */
    static BlockPos freeSpot(Minecraft mc) {
        BlockPos feet = mc.player.blockPosition();
        for (int radius = 1; radius <= 2; radius++) {
            for (int dy : new int[]{0, -1, 1}) {
                for (int dx = -radius; dx <= radius; dx++) {
                    for (int dz = -radius; dz <= radius; dz++) {
                        if (Math.max(Math.abs(dx), Math.abs(dz)) != radius) {
                            continue;
                        }
                        BlockPos pos = feet.offset(dx, dy, dz);
                        BlockState state = mc.level.getBlockState(pos);
                        BlockState under = mc.level.getBlockState(pos.below());
                        if (state.canBeReplaced() && state.getFluidState().isEmpty()
                                && under.isFaceSturdy(mc.level, pos.below(), Direction.UP)
                                && !pos.equals(feet) && !pos.equals(feet.above())) {
                            return pos;
                        }
                    }
                }
            }
        }
        return null;
    }
}

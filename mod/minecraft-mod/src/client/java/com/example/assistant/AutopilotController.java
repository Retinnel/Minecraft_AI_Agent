package com.example.assistant;

import net.minecraft.client.Minecraft;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.chat.Component;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.inventory.AbstractContainerMenu;
import net.minecraft.world.inventory.ClickType;
import net.minecraft.world.inventory.InventoryMenu;
import net.minecraft.world.inventory.Slot;
import net.minecraft.world.item.Item;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.Blocks;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * «Сделай сам» в моде: опрашивает сервер, исполняет по одной типизированной задаче и
 * отчитывается инвентарём. План и выбор задачи — на сервере; здесь только исполнение
 * задач из белого списка (добыть, изготовить, переплавить, поставить станцию, взять из
 * сундука). Чат и команды игры не используются.
 *
 * Остановка: клавиша K, любое движение игрока, опасность, тайм-аут задачи. Baritone
 * отменяется в тот же тик, сервер получает отчёт interrupted и ставит запуск на паузу.
 */
public final class AutopilotController {
    private static final int POLL_TICKS = 20;
    private static final int WAITING_POLL_TICKS = 40;
    // Блоки, которые Baritone не ломает даже ради пути: постройки и хранилища игрока.
    private static final List<Block> PROTECTED = List.of(Blocks.CHEST, Blocks.TRAPPED_CHEST, Blocks.BARREL,
            Blocks.ENDER_CHEST, Blocks.FURNACE, Blocks.CRAFTING_TABLE, Blocks.GLASS, Blocks.GLASS_PANE,
            Blocks.WHITE_BED, Blocks.RED_BED, Blocks.OAK_DOOR, Blocks.SPRUCE_DOOR, Blocks.BIRCH_DOOR,
            Blocks.IRON_DOOR, Blocks.TORCH, Blocks.WALL_TORCH);

    private final HttpAssistantClient http;
    private final AssistantConfig cfg;

    private volatile HttpAssistantClient.AutopilotBrief brief;
    private AutopilotTask task;
    private AutopilotTask.Execution execution;
    private long taskStartedMs;
    private int taskTicks;
    private int pollCountdown = POLL_TICKS;
    private volatile boolean requestInFlight;
    private boolean protectedSet;

    public AutopilotController(HttpAssistantClient http, AssistantConfig cfg) {
        this.http = http;
        this.cfg = cfg;
    }

    // --- состояние для чата и оверлея ---

    public HttpAssistantClient.AutopilotBrief getBrief() {
        return brief;
    }

    public boolean isActive() {
        String status = brief == null ? null : brief.status;
        return "running".equals(status) || "waiting".equals(status) || "pending_confirm".equals(status)
                || "paused".equals(status);
    }

    public String currentStep() {
        return task == null ? null : task.goal_step;
    }

    /** Свежий снимок запуска из ответа сервера: чат, опрос или /state. */
    public void onBrief(HttpAssistantClient.AutopilotBrief update) {
        if (update == null) {
            return;
        }
        brief = update;
        if (!"running".equals(update.status) && execution != null) {
            // Остановлено словами в чате — задачу прекращаем без отчёта: сервер уже знает.
            Minecraft mc = Minecraft.getInstance();
            mc.execute(() -> abort(mc));
        }
    }

    /** Мод может исполнять задачи: сообщается серверу с каждой репликой. */
    public static boolean singleplayer(Minecraft mc) {
        return mc.hasSingleplayerServer() && mc.getSingleplayerServer() != null
                && !mc.getSingleplayerServer().isPublished();
    }

    public void reset() {
        if (execution == null && brief == null) {
            return;
        }
        Minecraft mc = Minecraft.getInstance();
        if (execution != null) {
            abort(mc);
        }
        brief = null;
    }

    // --- тик ---

    public void tick(Minecraft mc) {
        LocalPlayer player = mc.player;
        if (player == null || mc.level == null) {
            return;
        }
        if (execution != null) {
            taskTicks++;
            AutopilotTask.Result result;
            if (!singleplayer(mc)) {
                // Мир открыли в LAN посреди запуска — дальше нельзя.
                result = AutopilotTask.Result.interrupted("blocked");
            } else {
                String unsafe = SafetyMonitor.check(mc, Math.max(1, cfg.autopilotMinHealth));
                if (unsafe != null) {
                    result = AutopilotTask.Result.interrupted(unsafe);
                } else if (taskTicks > Math.max(1, task.timeout_s) * 20) {
                    result = AutopilotTask.Result.failed("timeout");
                } else {
                    result = execution.tick(mc);
                }
            }
            if (result != null) {
                finish(mc, result);
            }
            return;
        }
        String status = brief == null ? null : brief.status;
        if (!"running".equals(status) && !"waiting".equals(status)) {
            return;
        }
        if (--pollCountdown > 0 || requestInFlight) {
            return;
        }
        pollCountdown = "waiting".equals(status) ? WAITING_POLL_TICKS : POLL_TICKS;
        requestInFlight = true;
        http.getAutopilotNext(brief.run_id, CraftingActions.nearbyStations(mc))
                .thenAccept(next -> mc.execute(() -> handleNext(mc, next)))
                .whenComplete((unused, error) -> requestInFlight = false);
    }

    /** Клавиша K или кнопка «Стоп» в оверлее. */
    public void stopByPlayer(Minecraft mc) {
        if (execution != null) {
            finish(mc, AutopilotTask.Result.interrupted(SafetyMonitor.PLAYER_INPUT));
            say(mc, "§e[AI] Автопилот остановлен клавишей.");
            return;
        }
        if (isActive()) {
            BaritoneBridge.cancel();
            http.postAutopilotStop(brief.run_id, "клавиша K")
                    .thenAccept(state -> mc.execute(() -> {
                        if (state != null && state.run != null) {
                            onBrief(state.briefFromRun());
                        }
                    }));
        }
    }

    // --- задачи ---

    private void handleNext(Minecraft mc, HttpAssistantClient.AutopilotNext next) {
        if (next == null || mc.player == null) {
            return;
        }
        HttpAssistantClient.AutopilotBrief current = brief == null ? new HttpAssistantClient.AutopilotBrief() : brief;
        current.status = next.status;
        if (next.message != null) {
            current.message = next.message;
        }
        brief = current;
        if (!"running".equals(next.status) || next.task == null || execution != null) {
            return;
        }
        start(mc, next.task);
    }

    private void start(Minecraft mc, AutopilotTask next) {
        task = next;
        taskTicks = 0;
        taskStartedMs = System.currentTimeMillis();
        if (!next.isKnownKind()) {
            // Неизвестный вид — отказ, ничего не исполняется.
            execution = null;
            report(mc, AutopilotTask.Result.failed("unsupported"));
            return;
        }
        if (!protectedSet && BaritoneBridge.isAvailable()) {
            BaritoneBridge.protect(PROTECTED);
            protectedSet = true;
        }
        execution = switch (next.kind) {
            case "mine" -> new MineExecution(mc, next);
            case "craft" -> CraftingActions.craft(mc, next);
            case "smelt" -> SmeltingActions.smelt(mc, next);
            case "place_station" -> CraftingActions.placeStation(mc, next);
            case "take_from_container" -> new TakeExecution(mc, next);
            default -> null;
        };
        if (execution == null) {
            report(mc, AutopilotTask.Result.failed("unsupported"));
        }
    }

    private void finish(Minecraft mc, AutopilotTask.Result result) {
        abortExecution(mc);
        report(mc, result);
    }

    private void abort(Minecraft mc) {
        abortExecution(mc);
        task = null;
    }

    private void abortExecution(Minecraft mc) {
        BaritoneBridge.cancel();
        if (execution != null) {
            try {
                execution.cancel(mc);
            } catch (RuntimeException error) {
                System.out.println("[AI Assistant] Автопилот: ошибка при отмене задачи: " + error);
            }
        }
        execution = null;
    }

    private void report(Minecraft mc, AutopilotTask.Result result) {
        if (task == null || mc.player == null) {
            return;
        }
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("run_id", brief == null ? null : brief.run_id);
        payload.put("task_id", task.task_id);
        payload.put("status", result.status());
        payload.put("reason", result.reason());
        payload.put("inventory", CraftingActions.inventoryCounts(mc.player));
        payload.put("stations", CraftingActions.nearbyStations(mc));
        BlockPos pos = mc.player.blockPosition();
        payload.put("position", Map.of("x", pos.getX(), "y", pos.getY(), "z", pos.getZ(),
                "dimension", mc.level.dimension().location().toString()));
        payload.put("elapsed_ms", System.currentTimeMillis() - taskStartedMs);
        task = null;
        requestInFlight = true;
        http.postAutopilotReport(payload)
                .thenAccept(next -> mc.execute(() -> handleNext(mc, next)))
                .whenComplete((unused, error) -> requestInFlight = false);
    }

    private static void say(Minecraft mc, String text) {
        if (mc.player != null) {
            mc.player.displayClientMessage(Component.literal(text), false);
        }
    }

    // --- добыча через Baritone ---

    private static final class MineExecution implements AutopilotTask.Execution {
        private final AutopilotTask task;
        private final Map<String, Integer> baseline;
        private boolean started;

        MineExecution(Minecraft mc, AutopilotTask task) {
            this.task = task;
            this.baseline = CraftingActions.inventoryCounts(mc.player);
        }

        @Override
        public AutopilotTask.Result tick(Minecraft mc) {
            if (!started) {
                List<Block> blocks = new ArrayList<>();
                for (String id : task.blocks == null ? List.<String>of() : task.blocks) {
                    ResourceLocation key = ResourceLocation.tryParse(id);
                    Block block = key == null ? null : BuiltInRegistries.BLOCK.getOptional(key).orElse(null);
                    if (block != null && block != Blocks.AIR && !PROTECTED.contains(block)) {
                        blocks.add(block);
                    }
                }
                if (!BaritoneBridge.isAvailable()) {
                    return AutopilotTask.Result.failed("no_baritone");
                }
                if (!BaritoneBridge.mine(Math.max(1, task.count), blocks, task.blockPos())) {
                    return AutopilotTask.Result.failed("not_found");
                }
                started = true;
                return null;
            }
            int gained = gained(mc);
            if (gained >= Math.max(1, task.count)) {
                return AutopilotTask.Result.done();
            }
            if (!BaritoneBridge.isBusy()) {
                // Baritone закончил или не нашёл блок: что добыто — то и засчитывается,
                // недостающее сервер пересчитает.
                return gained > 0 ? AutopilotTask.Result.done() : AutopilotTask.Result.failed("not_found");
            }
            return null;
        }

        private int gained(Minecraft mc) {
            int total = 0;
            for (Map.Entry<String, Integer> entry : CraftingActions.inventoryCounts(mc.player).entrySet()) {
                total += Math.max(0, entry.getValue() - baseline.getOrDefault(entry.getKey(), 0));
            }
            return total;
        }
    }

    // --- взять из своего сундука ---

    private static final class TakeExecution implements AutopilotTask.Execution {
        private enum Phase { WALK, WAIT_WALK, OPEN, WAIT_OPEN, TAKE }

        private final AutopilotTask task;
        private final int baseline;
        private Phase phase = Phase.WALK;
        private BlockPos chest;
        private int wait;

        TakeExecution(Minecraft mc, AutopilotTask task) {
            this.task = task;
            this.baseline = CraftingActions.countOf(mc.player, task.item);
        }

        @Override
        public AutopilotTask.Result tick(Minecraft mc) {
            LocalPlayer player = mc.player;
            switch (phase) {
                case WALK -> {
                    chest = task.blockPos();
                    if (chest == null) {
                        return AutopilotTask.Result.failed("not_found");
                    }
                    if (near(player, chest)) {
                        phase = Phase.OPEN;
                    } else if (BaritoneBridge.goNear(chest, 2)) {
                        phase = Phase.WAIT_WALK;
                        wait = 10;
                    } else {
                        // Без Baritone к сундуку не дойти — пусть игрок подойдёт сам.
                        return AutopilotTask.Result.failed("blocked");
                    }
                }
                case WAIT_WALK -> {
                    if (--wait <= 0 && !BaritoneBridge.isBusy()) {
                        if (!near(player, chest)) {
                            return AutopilotTask.Result.failed("blocked");
                        }
                        phase = Phase.OPEN;
                    }
                }
                case OPEN -> {
                    CraftingActions.useBlock(mc, chest);
                    phase = Phase.WAIT_OPEN;
                    wait = 40;
                }
                case WAIT_OPEN -> {
                    if (player.containerMenu != player.inventoryMenu) {
                        phase = Phase.TAKE;
                    } else if (--wait <= 0) {
                        return AutopilotTask.Result.failed("blocked");
                    }
                }
                case TAKE -> {
                    AbstractContainerMenu menu = player.containerMenu;
                    if (menu instanceof InventoryMenu) {
                        return AutopilotTask.Result.interrupted(SafetyMonitor.PLAYER_INPUT);
                    }
                    Item item = CraftingActions.item(task.item);
                    int need = baseline + Math.max(1, task.count);
                    for (Slot slot : menu.slots) {
                        if (CraftingActions.countOf(player, task.item) >= need) {
                            break;
                        }
                        if (!(slot.container instanceof Inventory) && item != null && slot.getItem().is(item)) {
                            CraftingActions.click(mc, menu, slot.index, 0, ClickType.QUICK_MOVE);
                        }
                    }
                    player.closeContainer();
                    int got = CraftingActions.countOf(player, task.item) - baseline;
                    return got > 0 ? AutopilotTask.Result.done() : AutopilotTask.Result.failed("not_found");
                }
            }
            return null;
        }

        private static boolean near(LocalPlayer player, BlockPos pos) {
            return player.blockPosition().distSqr(pos) <= 16;
        }

        @Override
        public void cancel(Minecraft mc) {
            if (mc.player != null && mc.player.containerMenu != mc.player.inventoryMenu) {
                mc.player.closeContainer();
            }
        }
    }
}

package com.example.assistant;

import net.minecraft.client.Minecraft;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.BlockPos;
import net.minecraft.world.inventory.AbstractFurnaceMenu;
import net.minecraft.world.inventory.ClickType;
import net.minecraft.world.item.Item;
import net.minecraft.world.level.block.Blocks;

/**
 * Переплавка в печи кликами по слотам: сырьё в слот 0, топливо в слот 1, результат из слота 2.
 * Печь должна стоять рядом — иначе сервер сначала выдаёт задачу её поставить.
 */
public final class SmeltingActions {
    private static final int INPUT = 0;
    private static final int FUEL = 1;
    private static final int RESULT = 2;

    private SmeltingActions() {
    }

    static AutopilotTask.Execution smelt(Minecraft mc, AutopilotTask task) {
        return new SmeltExecution(task, CraftingActions.countOf(mc.player, task.item));
    }

    private static final class SmeltExecution implements AutopilotTask.Execution {
        private enum Phase { OPEN, WAIT_OPEN, LOAD, COOK }

        private final AutopilotTask task;
        private final int baseline;
        private Phase phase = Phase.OPEN;
        private int wait;

        SmeltExecution(AutopilotTask task, int baseline) {
            this.task = task;
            this.baseline = baseline;
        }

        @Override
        public AutopilotTask.Result tick(Minecraft mc) {
            LocalPlayer player = mc.player;
            switch (phase) {
                case OPEN -> {
                    BlockPos furnace = CraftingActions.nearestBlock(mc, Blocks.FURNACE, CraftingActions.REACH);
                    if (furnace == null) {
                        return AutopilotTask.Result.failed("not_found");
                    }
                    CraftingActions.useBlock(mc, furnace);
                    phase = Phase.WAIT_OPEN;
                    wait = 40;
                }
                case WAIT_OPEN -> {
                    if (player.containerMenu instanceof AbstractFurnaceMenu) {
                        phase = Phase.LOAD;
                    } else if (--wait <= 0) {
                        return AutopilotTask.Result.failed("blocked");
                    }
                }
                case LOAD -> {
                    if (!(player.containerMenu instanceof AbstractFurnaceMenu menu)) {
                        return AutopilotTask.Result.interrupted(SafetyMonitor.PLAYER_INPUT);
                    }
                    Item input = CraftingActions.item(task.input);
                    Item fuel = CraftingActions.item(task.fuel);
                    if (input == null || fuel == null) {
                        return AutopilotTask.Result.failed("unsupported");
                    }
                    int count = Math.max(1, task.count);
                    if (CraftingActions.moveInto(mc, menu, stack -> stack.is(input), INPUT, count) < count) {
                        return AutopilotTask.Result.failed("blocked");
                    }
                    int fuelCount = Math.max(1, task.fuel_count);
                    if (CraftingActions.moveInto(mc, menu, stack -> stack.is(fuel), FUEL, fuelCount) < fuelCount) {
                        return AutopilotTask.Result.failed("blocked");
                    }
                    phase = Phase.COOK;
                }
                case COOK -> {
                    if (!(player.containerMenu instanceof AbstractFurnaceMenu menu)) {
                        return AutopilotTask.Result.interrupted(SafetyMonitor.PLAYER_INPUT);
                    }
                    if (!menu.getSlot(RESULT).getItem().isEmpty()) {
                        CraftingActions.click(mc, menu, RESULT, 0, ClickType.QUICK_MOVE);
                    }
                    if (CraftingActions.countOf(player, task.item) >= baseline + Math.max(1, task.count)) {
                        player.closeContainer();
                        return AutopilotTask.Result.done();
                    }
                }
            }
            return null;
        }

        @Override
        public void cancel(Minecraft mc) {
            if (mc.player != null && mc.player.containerMenu instanceof AbstractFurnaceMenu) {
                mc.player.closeContainer();
            }
        }
    }
}

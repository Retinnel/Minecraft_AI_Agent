package com.example.assistant;

import baritone.api.BaritoneAPI;
import baritone.api.IBaritone;
import baritone.api.Settings;
import baritone.api.pathing.goals.GoalNear;
import net.minecraft.core.BlockPos;
import net.minecraft.world.level.block.Block;

import java.util.ArrayList;
import java.util.List;

/**
 * Прямые вызовы API Baritone. Загружается только через {@link BaritoneBridge}, когда
 * Baritone установлен; больше нигде в моде типы Baritone не встречаются.
 */
final class BaritoneCalls {
    // Добыча идёт к известному месту первой: после прихода — уже обычный поиск вокруг.
    private static List<Block> pendingBlocks;
    private static int pendingCount;

    private BaritoneCalls() {
    }

    private static IBaritone primary() {
        return BaritoneAPI.getProvider().getPrimaryBaritone();
    }

    static boolean probe() {
        return primary() != null;
    }

    static void protect(List<Block> blocks) {
        Settings settings = BaritoneAPI.getSettings();
        List<Block> avoid = new ArrayList<>(settings.blocksToAvoidBreaking.value);
        for (Block block : blocks) {
            if (!avoid.contains(block)) {
                avoid.add(block);
            }
        }
        settings.blocksToAvoidBreaking.value = avoid;
        // Автопилот не перекладывает инвентарь игрока ради лучшего инструмента.
        settings.allowInventory.value = false;
    }

    static void mine(int count, List<Block> blocks, BlockPos goal) {
        IBaritone baritone = primary();
        if (goal != null) {
            pendingBlocks = blocks;
            pendingCount = count;
            baritone.getCustomGoalProcess().setGoalAndPath(new GoalNear(goal, 3));
            return;
        }
        baritone.getMineProcess().mine(count, blocks.toArray(new Block[0]));
    }

    static void goNear(BlockPos pos, int radius) {
        pendingBlocks = null;
        primary().getCustomGoalProcess().setGoalAndPath(new GoalNear(pos, radius));
    }

    static boolean isBusy() {
        IBaritone baritone = primary();
        boolean walking = baritone.getCustomGoalProcess().isActive();
        if (!walking && pendingBlocks != null) {
            // Дошли до известного места — теперь добыча.
            List<Block> blocks = pendingBlocks;
            pendingBlocks = null;
            baritone.getMineProcess().mine(pendingCount, blocks.toArray(new Block[0]));
            return true;
        }
        return walking || baritone.getMineProcess().isActive()
                || baritone.getPathingBehavior().isPathing();
    }

    static void cancel() {
        pendingBlocks = null;
        primary().getPathingBehavior().cancelEverything();
    }
}

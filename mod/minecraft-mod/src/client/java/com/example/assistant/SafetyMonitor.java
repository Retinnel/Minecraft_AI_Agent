package com.example.assistant;

import com.mojang.blaze3d.platform.InputConstants;
import net.minecraft.client.KeyMapping;
import net.minecraft.client.Minecraft;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.BlockPos;
import net.minecraft.tags.FluidTags;
import net.minecraft.world.entity.monster.Monster;

/**
 * Монитор опасности автопилота (B.8): здоровье, голод, враждебные мобы рядом, лава, ночь на
 * поверхности и ввод игрока. Любое срабатывание останавливает задачу: Baritone отменяется,
 * сервер получает отчёт interrupted и ставит запуск на паузу.
 */
public final class SafetyMonitor {
    public static final String DANGER = "danger";
    public static final String PLAYER_INPUT = "player_input";

    private static final int HOSTILE_RADIUS = 8;
    private static final int LAVA_RADIUS = 2;
    private static final int MIN_FOOD = 6;

    private SafetyMonitor() {
    }

    /** Причина остановки или null, если продолжать безопасно. */
    public static String check(Minecraft mc, int minHealth) {
        LocalPlayer player = mc.player;
        if (player == null || mc.level == null) {
            return DANGER;
        }
        if (playerMoves(mc)) {
            return PLAYER_INPUT;
        }
        if (player.getHealth() < minHealth || player.getFoodData().getFoodLevel() < MIN_FOOD) {
            return DANGER;
        }
        if (!mc.level.getEntitiesOfClass(Monster.class,
                player.getBoundingBox().inflate(HOSTILE_RADIUS)).isEmpty()) {
            return DANGER;
        }
        BlockPos feet = player.blockPosition();
        for (BlockPos pos : BlockPos.betweenClosed(feet.offset(-LAVA_RADIUS, -LAVA_RADIUS, -LAVA_RADIUS),
                feet.offset(LAVA_RADIUS, LAVA_RADIUS, LAVA_RADIUS))) {
            if (mc.level.getFluidState(pos).is(FluidTags.LAVA)) {
                return DANGER;
            }
        }
        if (mc.level.isNight() && mc.level.canSeeSky(feet)) {
            return DANGER;
        }
        return null;
    }

    /**
     * Игрок сам нажал движение. Читается состояние клавиатуры, а не KeyMapping: Baritone
     * управляет персонажем своим вводом, и его шаги не должны приниматься за игрока.
     */
    public static boolean playerMoves(Minecraft mc) {
        if (mc.screen != null) {
            return false;
        }
        long window = mc.getWindow().getWindow();
        KeyMapping[] keys = {mc.options.keyUp, mc.options.keyDown, mc.options.keyLeft,
                mc.options.keyRight, mc.options.keyJump};
        for (KeyMapping key : keys) {
            if (key.isUnbound()) {
                continue;
            }
            InputConstants.Key physical = InputConstants.getKey(key.saveString());
            if (physical.getType() == InputConstants.Type.KEYSYM
                    && InputConstants.isKeyDown(window, physical.getValue())) {
                return true;
            }
        }
        return false;
    }
}

package com.example.assistant;

import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.core.BlockPos;
import net.minecraft.world.level.block.Block;

import java.util.List;

/**
 * Единственная точка входа автопилота в Baritone.
 *
 * Baritone — необязательная внешняя зависимость (LGPL-3.0): в jar мода не входит, игрок
 * ставит его отдельно. Сам этот класс на типы Baritone не ссылается — они живут в
 * {@link BaritoneCalls}, который JVM загружает только при первом вызове. Поэтому без
 * Baritone мод грузится как обычно, а добыча просто недоступна: сервер просит игрока
 * принести нужное и ждёт.
 *
 * Текстовые команды Baritone («#mine») не используются: только API-вызовы с блоками,
 * которые выбрал решатель.
 */
public final class BaritoneBridge {
    private static Boolean available;

    private BaritoneBridge() {
    }

    /** Установлен ли Baritone и отвечает ли его API. */
    public static boolean isAvailable() {
        if (available == null) {
            boolean loaded = FabricLoader.getInstance().isModLoaded("baritone");
            if (loaded) {
                try {
                    loaded = BaritoneCalls.probe();
                } catch (Throwable error) {
                    System.out.println("[AI Assistant] Baritone найден, но API не отвечает: " + error);
                    loaded = false;
                }
            }
            available = loaded;
        }
        return available;
    }

    /** Защищённые блоки: Baritone не ломает их даже ради пути. */
    public static void protect(List<Block> blocks) {
        if (isAvailable()) {
            BaritoneCalls.protect(blocks);
        }
    }

    /** Добыть count блоков из списка; goal — известное по памяти место, к которому идти сначала. */
    public static boolean mine(int count, List<Block> blocks, BlockPos goal) {
        if (!isAvailable() || blocks.isEmpty()) {
            return false;
        }
        BaritoneCalls.mine(count, blocks, goal);
        return true;
    }

    /** Подойти к точке (сундук из памяти), остановиться в radius блоках. */
    public static boolean goNear(BlockPos pos, int radius) {
        if (!isAvailable()) {
            return false;
        }
        BaritoneCalls.goNear(pos, radius);
        return true;
    }

    public static boolean isBusy() {
        return isAvailable() && BaritoneCalls.isBusy();
    }

    /** Остановить всё, что делает Baritone, — стоп-клавиша, опасность, тайм-аут. */
    public static void cancel() {
        if (isAvailable()) {
            BaritoneCalls.cancel();
        }
    }
}

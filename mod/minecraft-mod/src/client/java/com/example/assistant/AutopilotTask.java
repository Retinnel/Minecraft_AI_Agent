package com.example.assistant;

import net.minecraft.core.BlockPos;

import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Задача автопилота от сервера — только данные. Мод сам выбирает действие по виду задачи;
 * вид не из белого списка отклоняется, текстовых команд в задаче нет и быть не может.
 */
public final class AutopilotTask {
    public static final Set<String> KINDS = Set.of("mine", "craft", "smelt", "place_station",
            "take_from_container");

    public String task_id;
    public String kind;
    public String item;
    public int count = 1;
    public List<String> blocks;
    public String recipe_id;
    public int runs = 1;
    public String station;
    public String input;
    public String fuel;
    public int fuel_count;
    public Map<String, Object> position;
    public String where;
    public int timeout_s = 60;
    public String goal_step;

    /** Итог задачи: done | failed | interrupted и причина из перечня TaskReport. */
    public record Result(String status, String reason) {
        public static Result done() {
            return new Result("done", null);
        }

        public static Result failed(String reason) {
            return new Result("failed", reason);
        }

        public static Result interrupted(String reason) {
            return new Result("interrupted", reason);
        }
    }

    /** Исполнение одной задачи: вызывается каждый тик, null — ещё в работе. */
    public interface Execution {
        Result tick(net.minecraft.client.Minecraft mc);

        default void cancel(net.minecraft.client.Minecraft mc) {
        }
    }

    public boolean isKnownKind() {
        return kind != null && KINDS.contains(kind);
    }

    /** Позиция из памяти мира, если сервер её прислал. */
    public BlockPos blockPos() {
        if (position == null) {
            return null;
        }
        Object x = position.get("x");
        Object y = position.get("y");
        Object z = position.get("z");
        if (!(x instanceof Number) || !(y instanceof Number) || !(z instanceof Number)) {
            return null;
        }
        return BlockPos.containing(((Number) x).doubleValue(), ((Number) y).doubleValue(),
                ((Number) z).doubleValue());
    }
}

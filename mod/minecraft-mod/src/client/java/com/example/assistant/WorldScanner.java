package com.example.assistant;

import net.minecraft.client.Minecraft;
import net.minecraft.client.multiplayer.ClientLevel;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.ResourceLocation;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Сканер ценных блоков вокруг игрока для памяти мира.
 *
 * Раз в интервал (по умолчанию 5 с) просматривает куб с ребром 2r+1 (по умолчанию r = 24)
 * вокруг игрока и ищет блоки из списка, полученного от сервера: руды, порталы, рассадники.
 * Куб просматривается не за один тик, а по горизонтальным слоям, по несколько слоёв за тик,
 * чтобы не просаживать частоту кадров. Незагруженные чанки пропускаются.
 *
 * Серверу уходят новые находки и места, где известный блок пропал (добыт), — так сервер
 * помечает запись устаревшей. Раз в минуту уходит полный список видимого, чтобы обновить
 * время наблюдения. Сканер только читает мир клиента и ничего в нём не меняет.
 */
public final class WorldScanner {
    private static final int LIST_REFRESH_TICKS = 1200;
    private static final int FULL_REPORT_CYCLES = 12;
    private static final int MAX_KNOWN = 8192;
    private static final int MAX_BATCH = 2000;

    private final HttpAssistantClient http;

    private volatile boolean enabled;
    private volatile Set<ResourceLocation> targets = Set.of();
    private volatile int radius = 24;
    private volatile int intervalTicks = 100;

    private int listCountdown = 1;
    private int cycleCountdown = 40;
    private int cycles;

    // Текущий проход: центр, измерение и следующий слой относительно центра.
    private BlockPos center;
    private String dimension;
    private int layer;
    private final Map<BlockPos, ResourceLocation> found = new HashMap<>();

    // Что сканер уже видел и сообщил: по нему находятся пропавшие блоки.
    private final Map<BlockPos, ResourceLocation> known = new HashMap<>();
    private String knownDimension;

    public WorldScanner(HttpAssistantClient http) {
        this.http = http;
    }

    /** Новый мир: прежние находки к нему не относятся. */
    public void reset() {
        center = null;
        found.clear();
        known.clear();
        knownDimension = null;
        cycleCountdown = 40;
        listCountdown = 1;
    }

    public void tick(Minecraft client) {
        if (--listCountdown <= 0) {
            listCountdown = LIST_REFRESH_TICKS;
            refreshList();
        }
        if (!enabled || targets.isEmpty() || client.level == null || client.player == null) {
            return;
        }
        if (center == null) {
            if (--cycleCountdown > 0) {
                return;
            }
            begin(client);
        }
        int layers = 2 * radius + 1;
        int perTick = Math.max(1, (int) Math.ceil(layers / (double) Math.max(1, intervalTicks / 2)));
        for (int i = 0; i < perTick && center != null; i++) {
            scanLayer(client.level);
        }
    }

    private void refreshList() {
        http.getScanList().thenAccept(list -> {
            if (list == null) {
                return;
            }
            Set<ResourceLocation> blocks = new HashSet<>();
            if (list.blocks != null) {
                for (String id : list.blocks) {
                    ResourceLocation location = ResourceLocation.tryParse(id);
                    if (location != null) {
                        blocks.add(location);
                    }
                }
            }
            targets = blocks;
            radius = Math.max(4, Math.min(48, list.radius <= 0 ? 24 : list.radius));
            intervalTicks = Math.max(20, Math.min(1200, (list.interval_s <= 0 ? 5 : list.interval_s) * 20));
            enabled = list.enabled;
        });
    }

    private void begin(Minecraft client) {
        center = client.player.blockPosition();
        dimension = client.level.dimension().location().toString();
        layer = -radius;
        found.clear();
        if (!dimension.equals(knownDimension)) {
            known.clear();
            knownDimension = dimension;
        }
    }

    private void scanLayer(ClientLevel level) {
        if (layer > radius) {
            finish();
            return;
        }
        int y = center.getY() + layer;
        layer += 1;
        if (level.isOutsideBuildHeight(y)) {
            return;
        }
        BlockPos.MutableBlockPos pos = new BlockPos.MutableBlockPos();
        for (int dx = -radius; dx <= radius; dx++) {
            int x = center.getX() + dx;
            for (int dz = -radius; dz <= radius; dz++) {
                int z = center.getZ() + dz;
                if (!level.getChunkSource().hasChunk(x >> 4, z >> 4)) {
                    continue;
                }
                pos.set(x, y, z);
                ResourceLocation id = BuiltInRegistries.BLOCK.getKey(level.getBlockState(pos).getBlock());
                if (targets.contains(id)) {
                    found.put(pos.immutable(), id);
                }
            }
        }
    }

    private boolean insideScan(BlockPos pos) {
        return Math.abs(pos.getX() - center.getX()) <= radius
                && Math.abs(pos.getY() - center.getY()) <= radius
                && Math.abs(pos.getZ() - center.getZ()) <= radius;
    }

    private void finish() {
        cycles += 1;
        boolean full = cycles % FULL_REPORT_CYCLES == 0;

        List<Map<String, Object>> blocks = new ArrayList<>();
        for (Map.Entry<BlockPos, ResourceLocation> entry : found.entrySet()) {
            if (blocks.size() >= MAX_BATCH) {
                break;
            }
            if (full || !entry.getValue().equals(known.get(entry.getKey()))) {
                blocks.add(point(entry.getKey(), entry.getValue().toString()));
            }
        }
        List<Map<String, Object>> removed = new ArrayList<>();
        List<BlockPos> gone = new ArrayList<>();
        for (BlockPos pos : known.keySet()) {
            if (insideScan(pos) && !found.containsKey(pos)) {
                gone.add(pos);
                if (removed.size() < MAX_BATCH) {
                    removed.add(point(pos, null));
                }
            }
        }
        gone.forEach(known::remove);
        if (known.size() + found.size() > MAX_KNOWN) {
            known.clear();
        }
        known.putAll(found);

        if (!blocks.isEmpty() || !removed.isEmpty()) {
            Map<String, Object> payload = new HashMap<>();
            payload.put("blocks", blocks);
            payload.put("removed", removed);
            http.postMemory("observations", payload);
        }
        center = null;
        cycleCountdown = Math.max(1, intervalTicks - (2 * radius + 1));
    }

    private Map<String, Object> point(BlockPos pos, String id) {
        Map<String, Object> point = new HashMap<>();
        if (id != null) {
            point.put("id", id);
        }
        point.put("dimension", dimension);
        point.put("x", pos.getX());
        point.put("y", pos.getY());
        point.put("z", pos.getZ());
        return point;
    }
}

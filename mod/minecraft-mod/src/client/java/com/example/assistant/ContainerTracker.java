package com.example.assistant;

import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.client.gui.screens.inventory.AbstractContainerScreen;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.inventory.AbstractContainerMenu;
import net.minecraft.world.inventory.ChestMenu;
import net.minecraft.world.inventory.ShulkerBoxMenu;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.HitResult;

import java.util.HashMap;
import java.util.Map;
import java.util.TreeMap;

/**
 * Учёт хранилищ для памяти мира: при закрытии сундука, бочки или шалкерового ящика серверу
 * уходят его координаты и содержимое. Блок хранилища определяется по тому, на что игрок
 * смотрел, когда открыл экран; содержимое читается из слотов самого хранилища, без
 * инвентаря игрока. Пока экран открыт, снимок обновляется каждый тик: после закрытия меню
 * уже пусто.
 */
public final class ContainerTracker {
    private final HttpAssistantClient http;

    private boolean open;
    private BlockPos pos;
    private String block;
    private String dimension;
    private Map<String, Integer> items;

    public ContainerTracker(HttpAssistantClient http) {
        this.http = http;
    }

    public void reset() {
        open = false;
        pos = null;
        items = null;
    }

    public void tick(Minecraft client) {
        Screen screen = client.screen;
        AbstractContainerMenu menu = screen instanceof AbstractContainerScreen<?> containerScreen
                ? containerScreen.getMenu() : null;
        int slots = storageSlots(menu);

        if (slots > 0) {
            if (!open) {
                open = true;
                pos = null;
                items = null;
                lookAtStorage(client);
            }
            if (pos != null) {
                items = read(menu, slots);
            }
            return;
        }
        if (open) {
            if (pos != null && items != null) {
                send();
            }
            reset();
        }
    }

    private void lookAtStorage(Minecraft client) {
        if (client.level == null || !(client.hitResult instanceof BlockHitResult hit)
                || hit.getType() != HitResult.Type.BLOCK) {
            return;
        }
        BlockPos target = hit.getBlockPos();
        String id = BuiltInRegistries.BLOCK.getKey(client.level.getBlockState(target).getBlock()).toString();
        if (!isStorage(id)) {
            return;
        }
        pos = target.immutable();
        block = id;
        dimension = client.level.dimension().location().toString();
    }

    static boolean isStorage(String id) {
        return id.equals("minecraft:chest") || id.equals("minecraft:trapped_chest")
                || id.equals("minecraft:barrel") || id.equals("minecraft:ender_chest")
                || id.endsWith("shulker_box");
    }

    private static int storageSlots(AbstractContainerMenu menu) {
        if (menu instanceof ChestMenu chest) {
            return chest.getRowCount() * 9;
        }
        if (menu instanceof ShulkerBoxMenu) {
            return 27;
        }
        return 0;
    }

    private static Map<String, Integer> read(AbstractContainerMenu menu, int slots) {
        Map<String, Integer> counts = new TreeMap<>();
        for (int i = 0; i < slots && i < menu.slots.size(); i++) {
            ItemStack stack = menu.getSlot(i).getItem();
            if (stack == null || stack.isEmpty()) {
                continue;
            }
            String id = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            counts.merge(id, stack.getCount(), Integer::sum);
        }
        return counts;
    }

    private void send() {
        Map<String, Object> payload = new HashMap<>();
        payload.put("block", block);
        payload.put("dimension", dimension);
        payload.put("x", pos.getX());
        payload.put("y", pos.getY());
        payload.put("z", pos.getZ());
        payload.put("items", items);
        http.postMemory("containers", payload);
    }
}

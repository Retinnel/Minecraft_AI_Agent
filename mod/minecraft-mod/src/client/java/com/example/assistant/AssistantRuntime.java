package com.example.assistant;

import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.chat.Component;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.EntityType;
import net.minecraft.world.entity.monster.Monster;
import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.phys.AABB;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.Vec3;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.TreeMap;

public final class AssistantRuntime {
    private final AssistantConfig cfg;
    private final HttpAssistantClient http;

    private int sendTickCountdown;
    private int adviceTickCountdown;

    private int settingsTickCountdown;
    private int runtimeSendIntervalTicks;

    private int hostileScanTickCountdown;
    private int inventoryScanTickCountdown;

    private long localTick;
    private long lastLowHealthTick;
    private long lastHungerLowTick;
    private long lastNightTick;
    private long lastNearHostileTick;

    private float lastKnownHealth = 20.0f;
    private long lastDeathTick = -1000;
    private static final long DEATH_COOLDOWN_TICKS = 1200;

    private boolean wasNight;
    private boolean rpRequestInFlight;
    private long rpPollingPausedUntilTick;

    private String lastAdviceShown;
    private String lastInventoryFingerprint = "";

    // Последний план и объяснение для оверлея. План в строку чата не помещается, поэтому
    // в чат уходит короткая сводка, а дерево целиком — сюда.
    private volatile String lastPlanTree;
    private volatile String lastPlanSummary;
    private volatile String lastPlanAlternatives;
    private volatile java.util.List<String> lastExplanation = java.util.List.of();
    private volatile String lastOutcome;
    private volatile String assistantMode = "creative";

    private boolean startupDone;

    public AssistantRuntime(AssistantConfig cfg) {
        this.cfg = cfg;
        this.http = new HttpAssistantClient(cfg);
        this.runtimeSendIntervalTicks = Math.max(1, cfg.sendIntervalTicks);
        this.sendTickCountdown = this.runtimeSendIntervalTicks;
        this.adviceTickCountdown = Math.max(1, cfg.adviceIntervalTicks);
        this.settingsTickCountdown = 1;

        this.hostileScanTickCountdown = 20;
        this.inventoryScanTickCountdown = 20;
    }

    /**
     * Возвращает последний полученный совет от AI-ассистента
     */
    public String getLastAdvice() {
        return lastAdviceShown;
    }

    /**
     * Возвращает HTTP клиент для запросов к серверу
     */
    public HttpAssistantClient getHttpClient() {
        return http;
    }

    public String getLastPlanTree() {
        return lastPlanTree;
    }

    public String getLastPlanSummary() {
        return lastPlanSummary;
    }

    public String getLastPlanAlternatives() {
        return lastPlanAlternatives;
    }

    public java.util.List<String> getLastExplanation() {
        return lastExplanation;
    }

    public String getLastOutcome() {
        return lastOutcome;
    }

    public String getAssistantMode() {
        return assistantMode;
    }

    public void setAssistantMode(String mode) {
        if (mode != null && !mode.isBlank()) {
            this.assistantMode = mode;
        }
    }

    /**
     * Запоминает то, что относится к оверлею: план, объяснение, режим.
     */
    private void rememberForOverlay(HttpAssistantClient.RPResponse rpResponse) {
        setAssistantMode(rpResponse.assistant_mode);
        if (rpResponse.outcome == null) {
            return;
        }
        lastOutcome = rpResponse.outcome;
        lastExplanation = rpResponse.explanation == null ? java.util.List.of() : rpResponse.explanation;
        if (rpResponse.plan_tree != null && !rpResponse.plan_tree.isBlank()) {
            lastPlanTree = rpResponse.plan_tree;
            lastPlanSummary = rpResponse.plan_summary;
            lastPlanAlternatives = rpResponse.plan_alternatives;
        }
    }

    public void onClientTick(Minecraft client) {
        if (client == null || client.player == null) {
            return;
        }

        localTick += 1;

        checkForDeath(client);

        if (!startupDone) {
            startupDone = true;
            sendStartup(client);
            sendInventorySnapshot(client, true);
            pullRPAndShow(client);
        }

        sendTickCountdown -= 1;
        adviceTickCountdown -= 1;
        settingsTickCountdown -= 1;
        hostileScanTickCountdown -= 1;
        inventoryScanTickCountdown -= 1;

        if (settingsTickCountdown <= 0) {
            // refresh server settings ~ every 5 seconds
            settingsTickCountdown = 100;
            pullSettings();
        }

        maybeSendMvpEvents(client);
        maybeSendTelemetryEvents(client);

        if (sendTickCountdown <= 0) {
            sendTickCountdown = Math.max(1, runtimeSendIntervalTicks);
            sendState(client);
        }

        if (adviceTickCountdown <= 0) {
            adviceTickCountdown = Math.max(1, cfg.adviceIntervalTicks);
            pullRPAndShow(client);
        }
    }

    private void maybeSendMvpEvents(Minecraft client) {
        float health = client.player.getHealth();
        int food = client.player.getFoodData().getFoodLevel();

        // low_health (throttle ~ 10s)
        if (health <= 6.0f && (localTick - lastLowHealthTick) >= 200) {
            lastLowHealthTick = localTick;

            Map<String, Object> payload = new HashMap<>();
            payload.put("event_type", "low_health");
            Map<String, Object> eventData = new HashMap<>();
            eventData.put("health", (double) health);
            eventData.put("food", food);
            payload.put("event_data", eventData);
            payload.put("level", "WARNING");
            payload.put("message", "low_health: hp=" + health + ", food=" + food);
            payload.put("player_health", (double) health);
            http.postLog(payload);
        }

        // hunger_low (throttle ~ 15s)
        if (food <= 8 && (localTick - lastHungerLowTick) >= 300) {
            lastHungerLowTick = localTick;

            Map<String, Object> payload = new HashMap<>();
            payload.put("event_type", "hunger_low");
            Map<String, Object> eventData = new HashMap<>();
            eventData.put("food", food);
            eventData.put("health", (double) health);
            payload.put("event_data", eventData);
            payload.put("level", "WARNING");
            payload.put("message", "hunger_low: food=" + food + ", hp=" + health);
            payload.put("player_health", (double) health);
            http.postLog(payload);
        }

        // night (send on transition day->night, throttle ~ 60s)
        if (client.level != null) {
            long dayTime = client.level.getDayTime() % 24000L;
            boolean isNight = dayTime >= 13000L && dayTime <= 23000L;

            if (isNight && !wasNight && (localTick - lastNightTick) >= 1200) {
                lastNightTick = localTick;

                Map<String, Object> payload = new HashMap<>();
                payload.put("event_type", "night");
                Map<String, Object> eventData = new HashMap<>();
                eventData.put("time", (double) dayTime);
                payload.put("event_data", eventData);
                payload.put("level", "INFO");
                payload.put("message", "night: time=" + dayTime);
                http.postLog(payload);
            }

            wasNight = isNight;
        }

        // near_hostile (scan ~ once per second, throttle sending ~ 5s)
        if (client.level != null && hostileScanTickCountdown <= 0) {
            hostileScanTickCountdown = 20;

            double radius = 14.0;
            AABB box = client.player.getBoundingBox().inflate(radius);
            List<Entity> entities = client.level.getEntities(client.player, box);

            int count = 0;
            List<String> types = new ArrayList<>();
            for (Entity e : entities) {
                if (e instanceof Monster && e.isAlive()) {
                    count += 1;
                    if (types.size() < 5) {
                        types.add(String.valueOf(e.getType()));
                    }
                }
            }

            if (count > 0 && (localTick - lastNearHostileTick) >= 100) {
                lastNearHostileTick = localTick;

                Map<String, Object> payload = new HashMap<>();
                payload.put("event_type", "near_hostile");
                Map<String, Object> eventData = new HashMap<>();
                eventData.put("radius", radius);
                eventData.put("count", count);
                eventData.put("types", types);
                payload.put("event_data", eventData);
                payload.put("level", "WARNING");
                payload.put("message", "near_hostile: count=" + count + ", r=" + radius);
                http.postLog(payload);
            }
        }
    }

    private void maybeSendTelemetryEvents(Minecraft client) {
        if (inventoryScanTickCountdown <= 0) {
            inventoryScanTickCountdown = 40;
            sendInventorySnapshot(client, false);
        }
    }

    private void sendStartup(Minecraft client) {
        float health = client.player.getHealth();
        int food = client.player.getFoodData().getFoodLevel();

        Map<String, Object> payload = new HashMap<>();
        payload.put("event_type", "startup");
        Map<String, Object> eventData = new HashMap<>();
        eventData.put("health", (double) health);
        eventData.put("food", food);
        payload.put("event_data", eventData);
        payload.put("level", "INFO");
        payload.put("message", "startup: connected. health=" + health + ", food=" + food);
        payload.put("player_health", (double) health);

        http.postLog(payload);
    }

    private void sendState(Minecraft client) {
        float health = client.player.getHealth();
        int food = client.player.getFoodData().getFoodLevel();

        Map<String, Object> payload = new HashMap<>();
        payload.put("event_type", "state");
        Map<String, Object> eventData = new HashMap<>();
        eventData.put("health", (double) health);
        eventData.put("food", food);
        payload.put("event_data", eventData);
        payload.put("level", health <= 6.0f ? "WARNING" : "INFO");
        payload.put("message", "state: health=" + health + ", food=" + food);
        payload.put("player_health", (double) health);

        http.postLog(payload);
    }

    private void sendInventorySnapshot(Minecraft client, boolean force) {
        if (client.player == null) {
            return;
        }

        Inventory inventory = client.player.getInventory();
        if (inventory == null) {
            return;
        }

        Map<String, Integer> counts = new TreeMap<>();
        List<Map<String, Object>> hotbar = new ArrayList<>();
        List<Map<String, Object>> armor = new ArrayList<>();
        List<Map<String, Object>> offhand = new ArrayList<>();

        for (ItemStack stack : inventory.items) {
            accumulateItemCount(counts, stack);
        }
        for (ItemStack stack : inventory.armor) {
            accumulateItemCount(counts, stack);
        }
        for (ItemStack stack : inventory.offhand) {
            accumulateItemCount(counts, stack);
        }

        for (int slot = 0; slot < Math.min(9, inventory.items.size()); slot++) {
            Map<String, Object> summary = summarizeStack(inventory.items.get(slot), "hotbar_" + slot);
            if (!summary.isEmpty()) {
                hotbar.add(summary);
            }
        }

        String[] armorSlots = {"boots", "leggings", "chestplate", "helmet"};
        for (int slot = 0; slot < inventory.armor.size(); slot++) {
            Map<String, Object> summary = summarizeStack(inventory.armor.get(slot), slot < armorSlots.length ? armorSlots[slot] : ("armor_" + slot));
            if (!summary.isEmpty()) {
                armor.add(summary);
            }
        }

        for (int slot = 0; slot < inventory.offhand.size(); slot++) {
            Map<String, Object> summary = summarizeStack(inventory.offhand.get(slot), slot == 0 ? "offhand" : ("offhand_" + slot));
            if (!summary.isEmpty()) {
                offhand.add(summary);
            }
        }

        Map<String, Object> eventData = new LinkedHashMap<>();
        eventData.put("counts", counts);
        eventData.put("hotbar", hotbar);
        eventData.put("armor", armor);
        eventData.put("offhand", offhand);

        String fingerprint = eventData.toString();
        if (!force && fingerprint.equals(lastInventoryFingerprint)) {
            return;
        }
        lastInventoryFingerprint = fingerprint;

        Map<String, Object> payload = new HashMap<>();
        payload.put("event_type", "inventory_snapshot");
        payload.put("event_data", eventData);
        payload.put("level", "INFO");
        payload.put("message", "inventory_snapshot: unique_items=" + counts.size());
        payload.put("player_health", (double) client.player.getHealth());
        http.postLog(payload);
    }

    private void accumulateItemCount(Map<String, Integer> counts, ItemStack stack) {
        String itemId = getItemId(stack);
        if (itemId == null) {
            return;
        }
        counts.put(itemId, counts.getOrDefault(itemId, 0) + stack.getCount());
    }

    private Map<String, Object> summarizeStack(ItemStack stack, String slotName) {
        String itemId = getItemId(stack);
        if (itemId == null) {
            return Map.of();
        }

        Map<String, Object> summary = new LinkedHashMap<>();
        summary.put("slot", slotName);
        summary.put("item_id", itemId);
        summary.put("display_name", stack.getHoverName().getString());
        summary.put("count", stack.getCount());
        return summary;
    }

    private String getItemId(ItemStack stack) {
        if (stack == null || stack.isEmpty()) {
            return null;
        }
        ResourceLocation key = BuiltInRegistries.ITEM.getKey(stack.getItem());
        return key == null ? null : key.toString();
    }

    private void pullRPAndShow(Minecraft client) {
        if (rpRequestInFlight || localTick < rpPollingPausedUntilTick) {
            return;
        }

        rpRequestInFlight = true;
        http.getRP()
                .thenAccept(rpResponse -> {
                    if (rpResponse == null) {
                        rpPollingPausedUntilTick = localTick + 100;
                        return;
                    }

                    client.execute(() -> {
                        if (client.player == null) {
                            return;
                        }

                        String msg = handleChatResponse(client, rpResponse);
                        if (msg == null || msg.isBlank()) {
                            rpPollingPausedUntilTick = localTick + 100;
                            return;
                        }
                        if (!"action".equalsIgnoreCase(safe(rpResponse.mode)) && msg.equals(lastAdviceShown)) {
                            return;
                        }

                        lastAdviceShown = msg;
                        client.player.displayClientMessage(Component.literal("[AI] " + msg), false);
                    });
                })
                .whenComplete((unused, error) -> rpRequestInFlight = false);
    }

    public void sendChatMessage(String text) {
        if (text == null || text.isBlank()) {
            System.out.println("[AI Assistant] sendChatMessage: text is null or blank");
            return;
        }

        System.out.println("[AI Assistant] sendChatMessage called with: " + text);
        
        http.sendChat(text).thenAccept(rpResponse -> {
            System.out.println("[AI Assistant] Chat response received: " + (rpResponse != null ? "not null" : "null"));

            if (rpResponse == null) {
                System.out.println("[AI Assistant] Chat response is null or empty!");
                return;
            }

            Minecraft mc = Minecraft.getInstance();
            mc.execute(() -> {
                if (mc.player == null) {
                    return;
                }

                rememberForOverlay(rpResponse);
                String msg = handleChatResponse(mc, rpResponse);
                if (msg == null || msg.isBlank()) {
                    System.out.println("[AI Assistant] Chat response text is empty after handling");
                    return;
                }

                System.out.println("[AI Assistant] Chat response: " + msg);
                mc.player.displayClientMessage(Component.literal("[AI] " + msg), false);
                if (rpResponse.plan_tree != null && !rpResponse.plan_tree.isBlank()) {
                    mc.player.displayClientMessage(
                            Component.literal("§7[AI] Подробный план — в оверлее, клавиша O"), false);
                }
            });
        }).exceptionally(ex -> {
            System.out.println("[AI Assistant] Chat request failed: " + ex);
            ex.printStackTrace();
            return null;
        });
    }

    private String handleChatResponse(Minecraft mc, HttpAssistantClient.RPResponse rpResponse) {
        if (!"action".equalsIgnoreCase(safe(rpResponse.mode))) {
            return firstNonBlank(rpResponse.response, rpResponse.error);
        }

        if (!rpResponse.execute) {
            return firstNonBlank(rpResponse.error, rpResponse.response, "Не удалось выполнить действие.");
        }

        String actionType = safe(rpResponse.action_type);
        if ("give".equalsIgnoreCase(actionType)) {
            return executeGiveCommand(mc, rpResponse);
        }
        if ("clear".equalsIgnoreCase(actionType)) {
            return executeClearCommand(mc, rpResponse);
        }
        if ("summon".equalsIgnoreCase(actionType)) {
            return executeSummonCommand(mc, rpResponse);
        }
        return firstNonBlank(rpResponse.error, "Я пока умею выполнять только /give, /clear и /summon.");
    }

    private String executeGiveCommand(Minecraft mc, HttpAssistantClient.RPResponse rpResponse) {
        String commandCapabilityError = validateCommandExecution(mc, "/give");
        if (!commandCapabilityError.isBlank()) {
            return commandCapabilityError;
        }

        String itemId = normalizeItemId(rpResponse.item_id);
        if (itemId == null) {
            return firstNonBlank(rpResponse.error, "Не удалось определить предмет для выдачи.");
        }

        ResourceLocation itemKey = ResourceLocation.tryParse(itemId);
        if (itemKey == null || !BuiltInRegistries.ITEM.containsKey(itemKey)) {
            return "Не могу выполнить /give: предмет '" + itemId + "' не найден.";
        }

        int count = normalizeItemCount(rpResponse.item_count);
        String command = safe(rpResponse.command);
        if (command.isBlank()) {
            command = "/give @s " + itemId + " " + count;
        }

        return sendCommand(mc, command, rpResponse.response);
    }

    private String executeSummonCommand(Minecraft mc, HttpAssistantClient.RPResponse rpResponse) {
        String commandCapabilityError = validateCommandExecution(mc, "/summon");
        if (!commandCapabilityError.isBlank()) {
            return commandCapabilityError;
        }

        String entityId = normalizeResourceId(rpResponse.entity_id);
        if (entityId == null) {
            return firstNonBlank(rpResponse.error, "Не удалось определить существо для призыва.");
        }

        ResourceLocation entityKey = ResourceLocation.tryParse(entityId);
        if (entityKey == null || !BuiltInRegistries.ENTITY_TYPE.containsKey(entityKey)) {
            return "Не могу выполнить /summon: существо '" + entityId + "' не найдено.";
        }

        int count = normalizeSummonCount(rpResponse.entity_count);
        SummonTarget target = resolveSummonTarget(mc);
        String coordinateText = formatCoordinate(target.x) + " " + formatCoordinate(target.y) + " " + formatCoordinate(target.z);

        StringBuilder builder = new StringBuilder();
        for (int idx = 0; idx < count; idx++) {
            if (idx > 0) {
                builder.append("\n");
            }
            builder.append("/summon ").append(entityId).append(" ").append(coordinateText);
        }
        String command = builder.toString();

        return sendCommand(mc, command, rpResponse.response);
    }

    private String executeClearCommand(Minecraft mc, HttpAssistantClient.RPResponse rpResponse) {
        String commandCapabilityError = validateCommandExecution(mc, "/clear");
        if (!commandCapabilityError.isBlank()) {
            return commandCapabilityError;
        }

        String itemId = normalizeItemId(rpResponse.item_id);
        if (itemId == null) {
            return firstNonBlank(rpResponse.error, "Не удалось определить предмет для удаления.");
        }

        ResourceLocation itemKey = ResourceLocation.tryParse(itemId);
        if (itemKey == null || !BuiltInRegistries.ITEM.containsKey(itemKey)) {
            return "Не могу выполнить /clear: предмет '" + itemId + "' не найден.";
        }

        int count = normalizeItemCount(rpResponse.item_count);
        String command = safe(rpResponse.command);
        if (command.isBlank()) {
            command = "/clear @s " + itemId + " " + count;
        }

        return sendCommand(mc, command, rpResponse.response);
    }

    private String validateCommandExecution(Minecraft mc, String commandName) {
        if (mc.player == null) {
            return "Игрок недоступен для выполнения команды.";
        }

        if (mc.player.connection == null) {
            return "Не могу отправить команду: соединение недоступно.";
        }

        if (!mc.player.hasPermissions(2)) {
            return "Не могу выполнить " + commandName + ": у игрока нет прав на команды.";
        }

        return "";
    }

    private String sendCommand(Minecraft mc, String command, String responseText) {
        String[] commands = command.split("\\r?\\n");
        String firstCommand = "";
        int sentCount = 0;
        for (String rawCommand : commands) {
            String trimmed = safe(rawCommand).trim();
            if (trimmed.isBlank()) {
                continue;
            }
            String normalizedCommand = trimmed.startsWith("/") ? trimmed.substring(1) : trimmed;
            if (firstCommand.isBlank()) {
                firstCommand = normalizedCommand;
            }
            mc.player.connection.sendCommand(normalizedCommand);
            sentCount += 1;
        }

        if (sentCount <= 1) {
            return firstNonBlank(responseText, "Выполняю: /" + firstCommand);
        }
        return firstNonBlank(responseText, "Выполняю " + sentCount + " команд.");
    }

    private String normalizeItemId(String itemId) {
        return normalizeResourceId(itemId);
    }

    private String normalizeResourceId(String resourceId) {
        String normalized = safe(resourceId).trim().toLowerCase(Locale.ROOT);
        if (normalized.isBlank()) {
            return null;
        }

        normalized = normalized.replace(" ", "_").replace("-", "_");
        if (!normalized.contains(":")) {
            normalized = "minecraft:" + normalized;
        }

        return normalized;
    }

    private int normalizeItemCount(int count) {
        if (count < 1) {
            return 1;
        }
        return Math.min(count, 64);
    }

    private int normalizeSummonCount(int count) {
        if (count < 1) {
            return 1;
        }
        return Math.min(count, 16);
    }

    private SummonTarget resolveSummonTarget(Minecraft mc) {
        if (mc.level != null && mc.hitResult instanceof BlockHitResult blockHit && blockHit.getType() == HitResult.Type.BLOCK) {
            Vec3 hitLocation = blockHit.getLocation();
            Vec3 directionOffset = Vec3.atLowerCornerOf(blockHit.getDirection().getNormal()).scale(0.75);
            Vec3 spawnLocation = hitLocation.add(directionOffset);
            return new SummonTarget(spawnLocation.x, spawnLocation.y, spawnLocation.z);
        }

        Vec3 fallback = mc.player.position().add(mc.player.getLookAngle().scale(2.0));
        return new SummonTarget(fallback.x, mc.player.getY(), fallback.z);
    }

    private String formatCoordinate(double value) {
        return String.format(Locale.ROOT, "%.2f", value);
    }

    private String firstNonBlank(String... values) {
        for (String value : values) {
            if (value != null && !value.isBlank()) {
                return value.trim();
            }
        }
        return "";
    }

    private String safe(String value) {
        return value == null ? "" : value;
    }

    private static final class SummonTarget {
        private final double x;
        private final double y;
        private final double z;

        private SummonTarget(double x, double y, double z) {
            this.x = x;
            this.y = y;
            this.z = z;
        }
    }

    private void pullSettings() {
        http.getSettings().thenAccept(settings -> {
            if (settings == null) {
                return;
            }

            int ms = settings.analysis_interval;
            if (ms < 100) {
                ms = 100;
            }
            if (ms > 60000) {
                ms = 60000;
            }

            int ticks = (int) Math.round(ms / 50.0);
            if (ticks < 1) {
                ticks = 1;
            }

            runtimeSendIntervalTicks = ticks;
        });
    }

    private void checkForDeath(Minecraft client) {
        if (client.player == null) {
            return;
        }

        float currentHealth = client.player.getHealth();

        if (lastKnownHealth > 0.0f && currentHealth <= 0.0f) {
            if ((localTick - lastDeathTick) >= DEATH_COOLDOWN_TICKS) {
                lastDeathTick = localTick;
                sendDeathEvent(client);
            }
        }

        lastKnownHealth = currentHealth;
    }

    private void sendDeathEvent(Minecraft client) {
        String cause = "unknown";
        try {
            if (client.player.getLastDamageSource() != null) {
                cause = client.player.getLastDamageSource().getMsgId();
            }
        } catch (Exception e) {
            System.out.println("[AI Assistant] Error getting death cause: " + e);
        }

        Map<String, Object> payload = new HashMap<>();
        payload.put("event_type", "death");
        Map<String, Object> eventData = new HashMap<>();
        eventData.put("cause", cause);
        payload.put("event_data", eventData);
        payload.put("level", "ERROR");
        payload.put("message", "death: cause=" + cause);
        payload.put("player_health", 0.0);

        http.postLog(payload);

        System.out.println("[AI Assistant] Death detected: " + cause);
    }
}

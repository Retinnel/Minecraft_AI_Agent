package com.example.assistant;

import com.google.gson.Gson;

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Map;
import java.util.concurrent.CompletableFuture;

public final class HttpAssistantClient {
    private static final Gson GSON = new Gson();
    private static final long RP_TIMEOUT_LOG_SUPPRESSION_MS = 30000L;

    private final HttpClient http;
    private final AssistantConfig cfg;
    private long lastRpTimeoutLogAtMs;

    public HttpAssistantClient(AssistantConfig cfg) {
        this.cfg = cfg;
        this.http = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(3))
                .version(HttpClient.Version.HTTP_1_1)
                .build();
    }

    private String baseUrl() {
        String url = cfg.serverUrl == null ? "" : cfg.serverUrl.trim();
        if (url.endsWith("/")) {
            url = url.substring(0, url.length() - 1);
        }
        return url;
    }

    public String getBaseUrl() {
        return baseUrl();
    }

    public CompletableFuture<Void> postLog(Map<String, Object> payload) {
        String json = GSON.toJson(payload);
        byte[] body = json.getBytes(StandardCharsets.UTF_8);

        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/logs/"))
                .timeout(Duration.ofSeconds(5))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofByteArray(body))
                .build();

        return http.sendAsync(req, HttpResponse.BodyHandlers.ofString())
                .thenAccept(resp -> {
                    int code = resp.statusCode();
                    if (code < 200 || code >= 300) {
                        String respBody = resp.body();
                        System.out.println("[AI Assistant] POST /api/logs/ failed: HTTP " + code + (respBody == null || respBody.isBlank() ? "" : (" body=" + respBody)));
                    }
                })
                .exceptionally(ex -> {
                    System.out.println("[AI Assistant] POST /api/logs/ exception: " + ex);
                    return null;
                });
    }

    public CompletableFuture<CraftRecipeResponse> getCraftRecipe(String query) {
        String q = query == null ? "" : query.trim();
        if (q.isEmpty()) {
            return CompletableFuture.completedFuture(null);
        }

        String encodedQuery;
        try {
            encodedQuery = URLEncoder.encode(q, StandardCharsets.UTF_8);
        } catch (Exception e) {
            encodedQuery = q;
        }

        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/wiki/craft?query=" + encodedQuery))
                .timeout(Duration.ofSeconds(8))
                .GET()
                .build();

        return http.sendAsync(req, HttpResponse.BodyHandlers.ofString())
                .thenApply(resp -> {
                    int code = resp.statusCode();
                    if (code < 200 || code >= 300) {
                        System.out.println("[AI Assistant] GET /api/wiki/craft failed: HTTP " + code);
                        return null;
                    }

                    try {
                        return GSON.fromJson(resp.body(), CraftRecipeResponse.class);
                    } catch (Exception e) {
                        System.out.println("[AI Assistant] GET /api/wiki/craft parse error: " + e);
                        return null;
                    }
                })
                .exceptionally(ex -> {
                    System.out.println("[AI Assistant] GET /api/wiki/craft exception: " + ex);
                    return null;
                });
    }

    public CompletableFuture<SettingsResponse> getSettings() {
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/settings/"))
                .timeout(Duration.ofSeconds(5))
                .GET()
                .build();

        return http.sendAsync(req, HttpResponse.BodyHandlers.ofString())
                .thenApply(resp -> {
                    int code = resp.statusCode();
                    if (code < 200 || code >= 300) {
                        System.out.println("[AI Assistant] GET /api/settings/ failed: HTTP " + code);
                        return null;
                    }

                    try {
                        return GSON.fromJson(resp.body(), SettingsResponse.class);
                    } catch (Exception e) {
                        System.out.println("[AI Assistant] GET /api/settings/ parse error: " + e);
                        return null;
                    }
                })
                .exceptionally(ex -> {
                    System.out.println("[AI Assistant] GET /api/settings/ exception: " + ex);
                    return null;
                });
    }

    public CompletableFuture<RPResponse> getRP() {
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/rp/"))
                .timeout(Duration.ofSeconds(15))
                .GET()
                .build();

        return http.sendAsync(req, HttpResponse.BodyHandlers.ofString())
                .thenApply(resp -> {
                    int code = resp.statusCode();
                    if (code < 200 || code >= 300) {
                        System.out.println("[AI Assistant] GET /api/rp/ failed: HTTP " + code);
                        return null;
                    }

                    try {
                        return GSON.fromJson(resp.body(), RPResponse.class);
                    } catch (Exception e) {
                        System.out.println("[AI Assistant] GET /api/rp/ parse error: " + e);
                        return null;
                    }
                })
                .exceptionally(ex -> {
                    if (hasCause(ex, HttpTimeoutException.class)) {
                        long now = System.currentTimeMillis();
                        if (now - lastRpTimeoutLogAtMs >= RP_TIMEOUT_LOG_SUPPRESSION_MS) {
                            lastRpTimeoutLogAtMs = now;
                            System.out.println("[AI Assistant] GET /api/rp/ timed out; suppressing repeated timeout logs for 30s.");
                        }
                    } else {
                        System.out.println("[AI Assistant] GET /api/rp/ exception: " + ex);
                    }
                    return null;
                });
    }

    private boolean hasCause(Throwable error, Class<? extends Throwable> type) {
        Throwable current = error;
        while (current != null) {
            if (type.isInstance(current)) {
                return true;
            }
            current = current.getCause();
        }
        return false;
    }

    public CompletableFuture<RPResponse> sendChat(String text) {
        return sendChat(text, Map.of());
    }

    /**
     * Реплика игрока и то, что игра знает о его взгляде: блок или существо под прицелом,
     * предмет в руке. Сервер по этим сведениям отвечает на «что это передо мной».
     */
    public CompletableFuture<RPResponse> sendChat(String text, Map<String, ?> gaze) {
        System.out.println("[AI Assistant] HttpAssistantClient.sendChat called with: " + text);
        
        Map<String, Object> payload = new java.util.LinkedHashMap<>();
        payload.put("text", text);
        if (gaze != null) {
            payload.putAll(gaze);
        }
        String json = GSON.toJson(payload);
        byte[] body = json.getBytes(StandardCharsets.UTF_8);
        
        System.out.println("[AI Assistant] Sending to: " + baseUrl() + "/api/rp/chat");
        System.out.println("[AI Assistant] Payload: " + json);

        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/rp/chat"))
                .timeout(Duration.ofSeconds(30))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofByteArray(body))
                .build();

        return http.sendAsync(req, HttpResponse.BodyHandlers.ofString())
                .thenApply(resp -> {
                    int code = resp.statusCode();
                    System.out.println("[AI Assistant] POST /api/rp/chat response code: " + code);
                    
                    if (code < 200 || code >= 300) {
                        System.out.println("[AI Assistant] POST /api/rp/chat failed: HTTP " + code);
                        System.out.println("[AI Assistant] Response body: " + resp.body());
                        return null;
                    }

                    try {
                        return GSON.fromJson(resp.body(), RPResponse.class);
                    } catch (Exception e) {
                        System.out.println("[AI Assistant] POST /api/rp/chat parse error: " + e);
                        return null;
                    }
                })
                .exceptionally(ex -> {
                    System.out.println("[AI Assistant] POST /api/rp/chat exception: " + ex);
                    ex.printStackTrace();
                    return null;
                });
    }

    /**
     * Текущий режим ассистента: creative или survival.
     */
    public CompletableFuture<ModeResponse> getMode() {
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/rp/mode"))
                .timeout(Duration.ofSeconds(5))
                .GET()
                .build();
        return sendForMode(req);
    }

    /**
     * Переключение режима. Режим меняет набор действий, доступных ассистенту, поэтому
     * хранится на сервере, а не в моде: в выживании сервер просто не предложит модели выдачу.
     */
    public CompletableFuture<ModeResponse> setMode(String mode) {
        String json = GSON.toJson(Map.of("mode", mode));
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/rp/mode"))
                .timeout(Duration.ofSeconds(5))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofByteArray(json.getBytes(StandardCharsets.UTF_8)))
                .build();
        return sendForMode(req);
    }

    private CompletableFuture<ModeResponse> sendForMode(HttpRequest req) {
        return http.sendAsync(req, HttpResponse.BodyHandlers.ofString())
                .thenApply(resp -> {
                    if (resp.statusCode() < 200 || resp.statusCode() >= 300) {
                        System.out.println("[AI Assistant] /api/rp/mode failed: HTTP " + resp.statusCode());
                        return null;
                    }
                    try {
                        return GSON.fromJson(resp.body(), ModeResponse.class);
                    } catch (Exception e) {
                        System.out.println("[AI Assistant] /api/rp/mode parse error: " + e);
                        return null;
                    }
                })
                .exceptionally(ex -> {
                    System.out.println("[AI Assistant] /api/rp/mode exception: " + ex);
                    return null;
                });
    }

    public CompletableFuture<CompactResponse> getCompactStatus() {
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/rp/compact"))
                .timeout(Duration.ofSeconds(5))
                .GET()
                .build();

        return http.sendAsync(req, HttpResponse.BodyHandlers.ofString())
                .thenApply(resp -> {
                    int code = resp.statusCode();
                    if (code < 200 || code >= 300) {
                        System.out.println("[AI Assistant] GET /api/rp/compact failed: HTTP " + code);
                        return null;
                    }

                    try {
                        return GSON.fromJson(resp.body(), CompactResponse.class);
                    } catch (Exception e) {
                        System.out.println("[AI Assistant] GET /api/rp/compact parse error: " + e);
                        return null;
                    }
                })
                .exceptionally(ex -> {
                    System.out.println("[AI Assistant] GET /api/rp/compact exception: " + ex);
                    return null;
                });
    }

    /**
     * Древо развития в сжатом виде: витрина по ступеням с состоянием узлов. Строение и
     * состояние выводит решатель на сервере из онтологии и снимка инвентаря.
     */
    public CompletableFuture<TreeResponse> getTree() {
        return getJson("/api/tree/compact", TreeResponse.class);
    }

    /** План до узла древа от текущего инвентаря — без обращения к языковой модели. */
    public CompletableFuture<TreePlanResponse> getTreePlan(String itemId) {
        String query = URLEncoder.encode(itemId == null ? "" : itemId, StandardCharsets.UTF_8);
        return getJson("/api/tree/plan?item=" + query, TreePlanResponse.class);
    }

    /**
     * Вход в мир: сервер поднимает его историю разговора, сессию, задания и кампанию и
     * возвращает последние сообщения и последний план.
     */
    public CompletableFuture<WorldResponse> enterWorld(String id, String name) {
        Map<String, String> payload = new java.util.LinkedHashMap<>();
        payload.put("id", id);
        payload.put("name", name);
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/worlds/enter"))
                .timeout(Duration.ofSeconds(5))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofByteArray(
                        GSON.toJson(payload).getBytes(StandardCharsets.UTF_8)))
                .build();
        return http.sendAsync(req, HttpResponse.BodyHandlers.ofString())
                .thenApply(resp -> {
                    if (resp.statusCode() < 200 || resp.statusCode() >= 300) {
                        System.out.println("[AI Assistant] /api/worlds/enter failed: HTTP " + resp.statusCode());
                        return (WorldResponse) null;
                    }
                    try {
                        return GSON.fromJson(resp.body(), WorldResponse.class);
                    } catch (Exception e) {
                        System.out.println("[AI Assistant] /api/worlds/enter parse error: " + e);
                        return (WorldResponse) null;
                    }
                })
                .exceptionally(ex -> {
                    System.out.println("[AI Assistant] /api/worlds/enter exception: " + ex);
                    return null;
                });
    }

    /** История разговора в текущем мире — для вкладки оверлея. */
    public CompletableFuture<WorldResponse> getWorldHistory(int limit) {
        return getJson("/api/worlds/history?limit=" + limit, WorldResponse.class);
    }

    public static final class WorldResponse {
        public WorldInfo world;
        public int messages;
        public java.util.List<WorldMessage> history;
        public WorldPlan plan;
        public String assistant_mode;

        public static final class WorldInfo {
            public String id;
            public String name;
            @com.google.gson.annotations.SerializedName("new")
            public boolean isNew;
        }

        public static final class WorldMessage {
            public String time;
            public String role;
            public String text;
            public String source;
        }

        public static final class WorldPlan {
            public String tree;
            public String summary;
            public String alternatives;
            public java.util.List<String> explanation;
        }
    }

    // --- память мира ---

    /** Какие блоки искать сканеру, радиус и частота: список задаёт сервер. */
    /** Факты памяти мира Minecraft; kind пустой — все виды. */
    public CompletableFuture<MemoryResponse> getMemory(String kind, int limit) {
        String query = "?game=Minecraft&limit=" + limit;
        if (kind != null && !kind.isEmpty()) {
            query += "&kind=" + kind;
        }
        return getJson("/api/memory/" + query, MemoryResponse.class);
    }

    public static final class MemoryResponse {
        public String game;
        public String world;
        public int total;
        public java.util.List<MemoryFact> facts;

        public static final class MemoryFact {
            public String id;
            public String kind;
            public String label;
            public String description;
            public String source;
        }
    }

    public CompletableFuture<ScanListResponse> getScanList() {
        return getJson("/api/memory/scan-list", ScanListResponse.class);
    }

    /** Наблюдения сканера или содержимое хранилища: path — observations или containers. */
    public CompletableFuture<Void> postMemory(String path, Map<String, Object> payload) {
        String route = "/api/memory/" + path;
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + route))
                .timeout(Duration.ofSeconds(5))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofByteArray(GSON.toJson(payload).getBytes(StandardCharsets.UTF_8)))
                .build();
        return http.sendAsync(req, HttpResponse.BodyHandlers.ofString())
                .thenAccept(resp -> {
                    if (resp.statusCode() < 200 || resp.statusCode() >= 300) {
                        System.out.println("[AI Assistant] POST " + route + " failed: HTTP " + resp.statusCode());
                    }
                })
                .exceptionally(ex -> {
                    System.out.println("[AI Assistant] POST " + route + " exception: " + ex);
                    return null;
                });
    }

    // --- «Сделай сам» ---

    /** Следующая задача автопилота; stations — станции, которые стоят рядом с игроком. */
    public CompletableFuture<AutopilotNext> getAutopilotNext(String runId, java.util.List<String> stations) {
        StringBuilder path = new StringBuilder("/api/autopilot/next?stations=")
                .append(java.net.URLEncoder.encode(String.join(",", stations), StandardCharsets.UTF_8));
        if (runId != null) {
            path.append("&run_id=").append(java.net.URLEncoder.encode(runId, StandardCharsets.UTF_8));
        }
        return getJson(path.toString(), AutopilotNext.class);
    }

    public CompletableFuture<AutopilotNext> postAutopilotReport(Map<String, Object> payload) {
        return postJson("/api/autopilot/report", payload, AutopilotNext.class);
    }

    public CompletableFuture<AutopilotState> postAutopilotStop(String runId, String reason) {
        Map<String, Object> payload = new java.util.LinkedHashMap<>();
        payload.put("run_id", runId);
        payload.put("reason", reason);
        return postJson("/api/autopilot/stop", payload, AutopilotState.class);
    }

    public CompletableFuture<AutopilotState> getAutopilotState() {
        return getJson("/api/autopilot/state", AutopilotState.class);
    }

    private <T> CompletableFuture<T> postJson(String path, Map<String, Object> payload, Class<T> type) {
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + path))
                .timeout(Duration.ofSeconds(10))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofByteArray(GSON.toJson(payload).getBytes(StandardCharsets.UTF_8)))
                .build();
        return http.sendAsync(req, HttpResponse.BodyHandlers.ofString())
                .thenApply(resp -> {
                    if (resp.statusCode() < 200 || resp.statusCode() >= 300) {
                        System.out.println("[AI Assistant] POST " + path + " failed: HTTP " + resp.statusCode());
                        return (T) null;
                    }
                    try {
                        return GSON.fromJson(resp.body(), type);
                    } catch (Exception e) {
                        System.out.println("[AI Assistant] POST " + path + " parse error: " + e);
                        return (T) null;
                    }
                })
                .exceptionally(ex -> {
                    System.out.println("[AI Assistant] POST " + path + " exception: " + ex);
                    return null;
                });
    }

    /** Коротко о запуске: для чата, оверлея и решения, опрашивать ли задачи. */
    public static final class AutopilotBrief {
        public String run_id;
        public String status;
        public String goal;
        public String label;
        public int step;
        public int total;
        public String current;
        public String message;
    }

    public static final class AutopilotNext {
        public String status;
        public AutopilotTask task;
        public String message;
    }

    public static final class AutopilotState {
        public AutopilotRun run;
        public AutopilotBrief brief;
        public boolean enabled;

        public AutopilotBrief briefFromRun() {
            if (brief != null || run == null) {
                return brief;
            }
            AutopilotBrief result = new AutopilotBrief();
            result.run_id = run.run_id;
            result.status = run.status;
            result.goal = run.goal;
            result.label = run.label;
            result.step = run.steps_done;
            result.total = Math.max(run.total_estimate, run.steps_done);
            result.message = run.message;
            return result;
        }
    }

    public static final class AutopilotRun {
        public String run_id;
        public String status;
        public String goal;
        public String label;
        public int steps_done;
        public int total_estimate;
        public String message;
    }

    public static final class ScanListResponse {
        public boolean enabled;
        public java.util.List<String> blocks;
        public int radius;
        public int interval_s;
    }

    private <T> CompletableFuture<T> getJson(String path, Class<T> type) {
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + path))
                .timeout(Duration.ofSeconds(5))
                .GET()
                .build();
        return http.sendAsync(req, HttpResponse.BodyHandlers.ofString())
                .thenApply(resp -> {
                    if (resp.statusCode() < 200 || resp.statusCode() >= 300) {
                        System.out.println("[AI Assistant] GET " + path + " failed: HTTP " + resp.statusCode());
                        return (T) null;
                    }
                    try {
                        return GSON.fromJson(resp.body(), type);
                    } catch (Exception e) {
                        System.out.println("[AI Assistant] GET " + path + " parse error: " + e);
                        return (T) null;
                    }
                })
                .exceptionally(ex -> {
                    System.out.println("[AI Assistant] GET " + path + " exception: " + ex);
                    return null;
                });
    }

    public static final class TreeResponse {
        public Integer rank;
        public java.util.Map<String, Integer> counts;
        public java.util.List<TierInfo> tiers;

        public static final class TierInfo {
            public int index;
            public String label;
            public boolean open;
            public java.util.List<NodeInfo> nodes;
        }

        public static final class NodeInfo {
            public String id;
            public String label;
            public String status;   // have | ready | open | locked | unknown
            public int steps;
        }
    }

    public static final class TreePlanResponse {
        public String item;
        public String label;
        public int have;
        public boolean feasible;
        public int steps;
        public String plan_tree;
        public String plan_summary;
        public String alternatives;
    }

    public static final class RPResponse {
        public String response;
        public double confidence;
        public String level;
        public String mode;
        public boolean execute;
        public String command;
        public String action_type;
        public String item_id;
        public int item_count;
        public String entity_id;
        public int entity_count;
        public String error;
        // Поля гибридного конвейера: исход, режим, план и объяснение.
        public String outcome;
        public String assistant_mode;
        public String plan_tree;
        public String plan_summary;
        public String plan_alternatives;
        public java.util.List<String> explanation;
        public java.util.List<String> options;
        // «Сделай сам»: состояние запуска после этого ответа.
        public AutopilotBrief autopilot;
    }

    public static final class ModeResponse {
        public boolean ok = true;
        public String mode;
        public String label;
        public String error;
    }

    public static final class SettingsResponse {
        public int analysis_interval;
    }

    public static final class CompactResponse {
        public String advice;
        public String advice_level;
        public Double health;
        public String last_event;
        public ChallengeInfo challenge;
        public CampaignInfo campaign;
        public ProgressInfo progression;
        public String timestamp;

        /** Ступень развития: выводится решателем по лучшей кирке в инвентаре. */
        public static final class ProgressInfo {
            public String label;
            public String tool_label;
            public String next_label;
            public Integer next_steps;
        }

        public static final class ChallengeInfo {
            public String title;
            public String status;
            public String progress;
            public String goal_type;
            public String reward;
        }

        /** Кампания: цель, персонаж и этапы с отметками — для вкладки оверлея. */
        public static final class CampaignInfo {
            public String title;
            public String persona_name;
            public String label;
            public String status;
            public Integer current;
            public Integer total;
            public String stage_label;
            public Integer stage_count;
            public Integer progress_count;
            public String reward_label;
            public Integer reward_count;
            public java.util.List<StageInfo> stages;
        }

        public static final class StageInfo {
            public int index;
            public String label;
            public int count;
            public String status;
            public int progress_count;
        }
    }

    public static final class CraftRecipeResponse {
        public String status;
        public String message;
        public Recipe recipe;

        public static final class Recipe {
            public String name;
            public String description;
            public String[][] grid;
        }
    }
}

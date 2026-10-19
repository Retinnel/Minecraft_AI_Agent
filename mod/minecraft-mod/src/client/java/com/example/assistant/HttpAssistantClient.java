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
        System.out.println("[AI Assistant] HttpAssistantClient.sendChat called with: " + text);
        
        Map<String, String> payload = Map.of("text", text);
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
        public String timestamp;

        public static final class ChallengeInfo {
            public String title;
            public String status;
            public String progress;
            public String goal_type;
            public String reward;
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

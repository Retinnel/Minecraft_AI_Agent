package com.example.assistant;

import net.minecraft.Util;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.GuiGraphics;
import net.minecraft.client.gui.components.Button;
import net.minecraft.client.gui.components.EditBox;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.network.chat.Component;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;

/**
 * Функциональный HUD оверлей AI-ассистента.
 * Отображает: здоровье, активный челлендж, последний совет, быстрые действия.
 * Открывается клавишей 'O'.
 */
public class AssistantOverlayScreen extends Screen {
    private static final Component TITLE = Component.literal("§l⚡ AI Assistant");
    
    private final AssistantRuntime runtime;
    private HttpAssistantClient.CompactResponse lastCompactData;
    private long lastDataFetchTime = 0;
    private Button refreshButton;
    private Button promptSettingsButton;
    private Button closeButton;
    private Button statusTabButton;
    private Button recipesTabButton;
    private Button planTabButton;
    private Button whyButton;
    private Button modeButton;

    // Вкладка плана: прокрутка длинного дерева и переключение на объяснение выбора.
    private int planScroll = 0;
    private boolean showWhy = false;

    private EditBox recipeQueryBox;
    private Button recipeSearchButton;
    private HttpAssistantClient.CraftRecipeResponse lastRecipeResponse;
    private String lastRecipeQuery;

    private enum Tab {
        STATUS,
        RECIPES,
        PLAN
    }

    private Tab activeTab = Tab.STATUS;
    
    // Цветовые коды
    private static final int COLOR_BG = 0xDD1a1a2e;         // Тёмно-синий фон
    private static final int COLOR_HEADER = 0xFF00d4aa;      // Бирюзовый заголовок
    private static final int COLOR_HEALTH_HIGH = 0xFF00ff88; // Зелёное здоровье
    private static final int COLOR_HEALTH_MED = 0xFFffaa00;  // Жёлтое здоровье
    private static final int COLOR_HEALTH_LOW = 0xFFff4444;  // Красное здоровье
    private static final int COLOR_TEXT = 0xFFeeeeee;        // Белый текст
    private static final int COLOR_MUTED = 0xFF888888;      // Серый текст
    private static final int COLOR_ACCENT = 0xFFff6b6b;     // Акцент красный
    private static final int COLOR_SUCCESS = 0xFF4ecdc4;    // Успех бирюзовый
    private static final int COLOR_GOLD = 0xFFffd93d;       // Золотой для наград

    // Маппинг русских названий предметов на Minecraft Item для отрисовки иконок
    private static final java.util.Map<String, Item> RECIPE_ITEM_MAP = new java.util.HashMap<>();
    static {
        // Базовые материалы
        RECIPE_ITEM_MAP.put("пусто", null);
        RECIPE_ITEM_MAP.put("доска", Items.OAK_PLANKS);
        RECIPE_ITEM_MAP.put("доски", Items.OAK_PLANKS);
        RECIPE_ITEM_MAP.put("бревно", Items.OAK_LOG);
        RECIPE_ITEM_MAP.put("палка", Items.STICK);
        RECIPE_ITEM_MAP.put("палки", Items.STICK);
        RECIPE_ITEM_MAP.put("булыжник", Items.COBBLESTONE);
        RECIPE_ITEM_MAP.put("каменная руда", Items.STONE);
        RECIPE_ITEM_MAP.put("камень", Items.STONE);
        RECIPE_ITEM_MAP.put("железный слиток", Items.IRON_INGOT);
        RECIPE_ITEM_MAP.put("золотой слиток", Items.GOLD_INGOT);
        RECIPE_ITEM_MAP.put("алмаз", Items.DIAMOND);
        RECIPE_ITEM_MAP.put("кожа", Items.LEATHER);
        RECIPE_ITEM_MAP.put("паутина", Items.STRING);
        RECIPE_ITEM_MAP.put("нить", Items.STRING);
        RECIPE_ITEM_MAP.put("кремень", Items.FLINT);
        RECIPE_ITEM_MAP.put("перо", Items.FEATHER);
        RECIPE_ITEM_MAP.put("уголь", Items.COAL);
        RECIPE_ITEM_MAP.put("факел", Items.TORCH);
        RECIPE_ITEM_MAP.put("слизь", Items.SLIME_BALL);
        RECIPE_ITEM_MAP.put("поршень", Items.PISTON);
        RECIPE_ITEM_MAP.put("редстоун пыль", Items.REDSTONE);
        RECIPE_ITEM_MAP.put("красная пыль", Items.REDSTONE);
        RECIPE_ITEM_MAP.put("кварц", Items.QUARTZ);
        RECIPE_ITEM_MAP.put("адский кирпич", Items.NETHER_BRICK);
        RECIPE_ITEM_MAP.put("семя тыквы", Items.PUMPKIN_SEEDS);
        RECIPE_ITEM_MAP.put("снежок", Items.SNOWBALL);
        RECIPE_ITEM_MAP.put("глина", Items.CLAY_BALL);
        RECIPE_ITEM_MAP.put("книга", Items.BOOK);
        RECIPE_ITEM_MAP.put("порошок ферментации", Items.BLAZE_POWDER);
        RECIPE_ITEM_MAP.put("песок", Items.SAND);
        RECIPE_ITEM_MAP.put("гравий", Items.GRAVEL);
        RECIPE_ITEM_MAP.put("шерсть", Items.WHITE_WOOL);
        RECIPE_ITEM_MAP.put("красная шерсть", Items.RED_WOOL);
        RECIPE_ITEM_MAP.put("смычок", Items.BOW);
        RECIPE_ITEM_MAP.put("сундук", Items.CHEST);
        RECIPE_ITEM_MAP.put("доска тёмного дуба", Items.DARK_OAK_PLANKS);
        RECIPE_ITEM_MAP.put("земля", Items.DIRT);
        RECIPE_ITEM_MAP.put("кирпич", Items.BRICK);
        RECIPE_ITEM_MAP.put("стекло", Items.GLASS);
        RECIPE_ITEM_MAP.put("изумруд", Items.EMERALD);
        RECIPE_ITEM_MAP.put("железный блок", Items.IRON_BLOCK);
        RECIPE_ITEM_MAP.put("алмазный блок", Items.DIAMOND_BLOCK);
        RECIPE_ITEM_MAP.put("золотой блок", Items.GOLD_BLOCK);
        RECIPE_ITEM_MAP.put("лазурит", Items.LAPIS_LAZULI);
        RECIPE_ITEM_MAP.put("костная мука", Items.BONE_MEAL);
        RECIPE_ITEM_MAP.put("кость", Items.BONE);
        RECIPE_ITEM_MAP.put("паучий глаз", Items.SPIDER_EYE);
        RECIPE_ITEM_MAP.put("железная руда", Items.RAW_IRON);
        // Английские fallback
        RECIPE_ITEM_MAP.put("stick", Items.STICK);
        RECIPE_ITEM_MAP.put("planks", Items.OAK_PLANKS);
        RECIPE_ITEM_MAP.put("oak planks", Items.OAK_PLANKS);
        RECIPE_ITEM_MAP.put("log", Items.OAK_LOG);
        RECIPE_ITEM_MAP.put("oak log", Items.OAK_LOG);
        RECIPE_ITEM_MAP.put("cobblestone", Items.COBBLESTONE);
        RECIPE_ITEM_MAP.put("stone", Items.STONE);
        RECIPE_ITEM_MAP.put("iron ingot", Items.IRON_INGOT);
        RECIPE_ITEM_MAP.put("gold ingot", Items.GOLD_INGOT);
        RECIPE_ITEM_MAP.put("diamond", Items.DIAMOND);
        RECIPE_ITEM_MAP.put("coal", Items.COAL);
        RECIPE_ITEM_MAP.put("torch", Items.TORCH);
        RECIPE_ITEM_MAP.put("redstone", Items.REDSTONE);
        RECIPE_ITEM_MAP.put("string", Items.STRING);
        RECIPE_ITEM_MAP.put("flint", Items.FLINT);
        RECIPE_ITEM_MAP.put("feather", Items.FEATHER);
        RECIPE_ITEM_MAP.put("leather", Items.LEATHER);
        RECIPE_ITEM_MAP.put("slimeball", Items.SLIME_BALL);
        RECIPE_ITEM_MAP.put("piston", Items.PISTON);
        RECIPE_ITEM_MAP.put("quartz", Items.QUARTZ);
        RECIPE_ITEM_MAP.put("nether brick", Items.NETHER_BRICK);
        RECIPE_ITEM_MAP.put("clay", Items.CLAY_BALL);
        RECIPE_ITEM_MAP.put("book", Items.BOOK);
        RECIPE_ITEM_MAP.put("sand", Items.SAND);
        RECIPE_ITEM_MAP.put("gravel", Items.GRAVEL);
        RECIPE_ITEM_MAP.put("wool", Items.WHITE_WOOL);
        RECIPE_ITEM_MAP.put("bow", Items.BOW);
        RECIPE_ITEM_MAP.put("chest", Items.CHEST);
        RECIPE_ITEM_MAP.put("dirt", Items.DIRT);
        RECIPE_ITEM_MAP.put("glass", Items.GLASS);
        RECIPE_ITEM_MAP.put("emerald", Items.EMERALD);
        RECIPE_ITEM_MAP.put("lapis lazuli", Items.LAPIS_LAZULI);
        RECIPE_ITEM_MAP.put("obsidian", Items.OBSIDIAN);
        RECIPE_ITEM_MAP.put("bucket", Items.BUCKET);
        RECIPE_ITEM_MAP.put("water bucket", Items.WATER_BUCKET);
        RECIPE_ITEM_MAP.put("egg", Items.EGG);
        RECIPE_ITEM_MAP.put("sugar", Items.SUGAR);
        RECIPE_ITEM_MAP.put("wheat", Items.WHEAT);
        RECIPE_ITEM_MAP.put("paper", Items.PAPER);
        RECIPE_ITEM_MAP.put("gunpowder", Items.GUNPOWDER);
        RECIPE_ITEM_MAP.put("ender pearl", Items.ENDER_PEARL);
        RECIPE_ITEM_MAP.put("blaze powder", Items.BLAZE_POWDER);
        RECIPE_ITEM_MAP.put("blaze rod", Items.BLAZE_ROD);
        RECIPE_ITEM_MAP.put("snowball", Items.SNOWBALL);
        RECIPE_ITEM_MAP.put("fermented spider eye", Items.FERMENTED_SPIDER_EYE);
        RECIPE_ITEM_MAP.put("pumpkin seeds", Items.PUMPKIN_SEEDS);
        RECIPE_ITEM_MAP.put("raw iron", Items.RAW_IRON);
        RECIPE_ITEM_MAP.put("raw gold", Items.RAW_GOLD);
        RECIPE_ITEM_MAP.put("copper ingot", Items.COPPER_INGOT);
        RECIPE_ITEM_MAP.put("amethyst shard", Items.AMETHYST_SHARD);
        RECIPE_ITEM_MAP.put("bone", Items.BONE);
        RECIPE_ITEM_MAP.put("bone meal", Items.BONE_MEAL);
        RECIPE_ITEM_MAP.put("spider eye", Items.SPIDER_EYE);
        RECIPE_ITEM_MAP.put("rotten flesh", Items.ROTTEN_FLESH);
        RECIPE_ITEM_MAP.put("magma cream", Items.MAGMA_CREAM);
    }

    public AssistantOverlayScreen(AssistantRuntime runtime) {
        super(TITLE);
        this.runtime = runtime;
        fetchCompactData();
    }
    
    // Ширина панели одна на все вкладки: дереву плана нужно больше места, чем статусу.
    private int panelWidth() {
        return Math.min(this.width - 40, 460);
    }

    @Override
    protected void init() {
        super.init();

        int panelW = panelWidth();
        int panelH = 220;
        int left = (this.width - panelW) / 2;
        int top = (this.height - panelH) / 2;

        // Вкладки
        int tabY = top - 24;
        int tabW = (panelW - 8) / 3;
        statusTabButton = Button.builder(Component.literal("§fСтатус"), btn -> {
            setActiveTab(Tab.STATUS);
        }).bounds(left, tabY, tabW, 20).build();

        recipesTabButton = Button.builder(Component.literal("§fРецепты"), btn -> {
            setActiveTab(Tab.RECIPES);
        }).bounds(left + tabW + 4, tabY, tabW, 20).build();

        planTabButton = Button.builder(Component.literal("§fПлан"), btn -> {
            setActiveTab(Tab.PLAN);
        }).bounds(left + (tabW + 4) * 2, tabY, tabW, 20).build();

        addRenderableWidget(statusTabButton);
        addRenderableWidget(recipesTabButton);
        addRenderableWidget(planTabButton);

        whyButton = Button.builder(Component.literal(whyLabel()), btn -> {
            showWhy = !showWhy;
            planScroll = 0;
            btn.setMessage(Component.literal(whyLabel()));
        }).bounds(left + panelW - 130, top + 30, 115, 18).build();
        addRenderableWidget(whyButton);
        
        int btnY = this.height - 40;
        int btnWidth = 95;
        int spacing = 8;
        int startX = (this.width - (btnWidth * 4 + spacing * 3)) / 2;
        
        // Кнопка обновления
        refreshButton = Button.builder(Component.literal("§b🔄 Обновить"), btn -> {
            fetchCompactData();
        }).bounds(startX, btnY, btnWidth, 20).build();

        promptSettingsButton = Button.builder(Component.literal("§6⚙ Промпты"), btn -> {
            Util.getPlatform().openUri(runtime.getHttpClient().getBaseUrl() + "/dashboard#settings");
        }).bounds(startX + btnWidth + spacing, btnY, btnWidth, 20).build();
        
        // Кнопка закрытия
        closeButton = Button.builder(Component.literal("§c✕ Закрыть"), btn -> {
            onClose();
        }).bounds(startX + (btnWidth + spacing) * 3, btnY, btnWidth, 20).build();

        // Переключатель режима. Режим хранится на сервере: от него зависит, какие действия
        // вообще предъявляются модели.
        modeButton = Button.builder(Component.literal(modeLabel()), btn -> {
            toggleMode();
        }).bounds(startX + (btnWidth + spacing) * 2, btnY, btnWidth, 20).build();
        
        addRenderableWidget(refreshButton);
        addRenderableWidget(promptSettingsButton);
        addRenderableWidget(modeButton);
        addRenderableWidget(closeButton);
        refreshMode();

        // Recipes controls (rendered + enabled only on Recipes tab)
        recipeQueryBox = new EditBox(
                this.font,
                left + 15,
                top + 42,
                220,
                18,
                Component.literal("Поиск рецепта")
        );
        recipeQueryBox.setMaxLength(64);
        recipeQueryBox.setValue(lastRecipeQuery == null ? "" : lastRecipeQuery);
        addRenderableWidget(recipeQueryBox);

        recipeSearchButton = Button.builder(Component.literal("§bНайти"), btn -> {
            searchRecipe();
        }).bounds(left + 245, top + 42, 80, 18).build();
        addRenderableWidget(recipeSearchButton);

        applyTabVisibility();
    }

    private void setActiveTab(Tab tab) {
        if (tab == null) {
            tab = Tab.STATUS;
        }
        this.activeTab = tab;
        applyTabVisibility();
    }

    private void applyTabVisibility() {
        boolean isStatus = activeTab == Tab.STATUS;
        boolean isPlan = activeTab == Tab.PLAN;
        boolean isRecipes = activeTab == Tab.RECIPES;
        if (whyButton != null) {
            whyButton.visible = isPlan;
            whyButton.active = isPlan;
        }
        if (planTabButton != null) {
            planTabButton.setMessage(Component.literal(isPlan ? "§b§lПлан" : "§fПлан"));
        }
        if (refreshButton != null) {
            refreshButton.visible = isStatus;
            refreshButton.active = isStatus;
        }
        if (recipeQueryBox != null) {
            recipeQueryBox.visible = isRecipes;
            recipeQueryBox.setEditable(isRecipes);
        }
        if (recipeSearchButton != null) {
            recipeSearchButton.visible = isRecipes;
            recipeSearchButton.active = isRecipes;
        }

        if (statusTabButton != null) {
            statusTabButton.setMessage(Component.literal(isStatus ? "§b§lСтатус" : "§fСтатус"));
        }
        if (recipesTabButton != null) {
            recipesTabButton.setMessage(Component.literal(isRecipes ? "§b§lРецепты" : "§fРецепты"));
        }

        // Автофокус на поле ввода при открытии вкладки "Рецепты"
        if (isRecipes && recipeQueryBox != null) {
            this.setFocused(recipeQueryBox);
            recipeQueryBox.setFocused(true);
        } else if (recipeQueryBox != null) {
            recipeQueryBox.setFocused(false);
        }
    }
    
    @Override
    public void render(GuiGraphics graphics, int mouseX, int mouseY, float partialTick) {
        // Фон с градиентным эффектом
        renderBackground(graphics);
        
        // Основная панель (центрированная)
        int panelW = panelWidth();
        int panelH = 220;
        int left = (this.width - panelW) / 2;
        int top = (this.height - panelH) / 2;
        
        // Тень
        graphics.fill(left + 4, top + 4, left + panelW + 4, top + panelH + 4, 0x55000000);
        // Основной фон
        graphics.fill(left, top, left + panelW, top + panelH, COLOR_BG);
        // Рамка
        graphics.hLine(left, left + panelW, top, COLOR_HEADER);
        graphics.hLine(left, left + panelW, top + panelH - 1, COLOR_HEADER);
        graphics.vLine(left, top, top + panelH, COLOR_HEADER);
        graphics.vLine(left + panelW - 1, top, top + panelH, COLOR_HEADER);
        
        // Заголовок
        graphics.drawCenteredString(font, "§l⚡ AI Assistant HUD", this.width / 2, top + 10, COLOR_HEADER);
        graphics.hLine(left + 10, left + panelW - 10, top + 25, 0xFF444466);

        if (activeTab == Tab.STATUS) {
            renderStatusTab(graphics, left, top, panelW, panelH);
        } else if (activeTab == Tab.PLAN) {
            renderPlanTab(graphics, left, top, panelW, panelH);
        } else {
            renderRecipesTab(graphics, left, top, panelW, panelH);
        }
        
        // Кнопки
        super.render(graphics, mouseX, mouseY, partialTick);
    }

    private void renderStatusTab(GuiGraphics graphics, int left, int top, int panelW, int panelH) {
        int contentY = top + 35;
        int col1X = left + 15;
        int col2X = left + 175;
        int positionY = contentY + 32;

        // === КОЛОНКА 1: Статус игрока ===
        graphics.drawString(font, "§l❤ Здоровье", col1X, contentY, COLOR_ACCENT);
        renderHealthBar(graphics, col1X, contentY + 12, lastCompactData != null ? lastCompactData.health : null);

        graphics.drawString(font, "§l📊 Событие", col1X, contentY + 35, COLOR_SUCCESS);
        String event = lastCompactData != null && lastCompactData.last_event != null
                ? lastCompactData.last_event
                : "—";
        graphics.drawString(font, "   " + event, col1X, contentY + 47, COLOR_TEXT);

        // === КОЛОНКА 2: Челлендж ===
        graphics.drawString(font, "§l🎯 Челлендж", col2X, contentY, COLOR_GOLD);
        if (lastCompactData != null && lastCompactData.challenge != null) {
            HttpAssistantClient.CompactResponse.ChallengeInfo ch = lastCompactData.challenge;
            String title = ch.title != null ? ch.title : "Без названия";
            String status = ch.status != null ? ch.status : "—";
            String progress = ch.progress != null ? ch.progress : "—";
            String[] titleLines = wrapText(title, 16);

            int statusColor = "completed".equals(status) ? COLOR_SUCCESS :
                    "active".equals(status) ? COLOR_HEALTH_HIGH : COLOR_MUTED;

            int challengeLineY = contentY + 12;
            for (String titleLine : titleLines) {
                graphics.drawString(font, "   " + titleLine, col2X, challengeLineY, COLOR_TEXT);
                challengeLineY += 11;
            }
            graphics.drawString(font, "   §7Статус: §r" + status, col2X, challengeLineY, statusColor);
            challengeLineY += 12;
            graphics.drawString(font, "   §7Прогресс: §r" + progress, col2X, challengeLineY, COLOR_HEALTH_HIGH);
            positionY = challengeLineY + 16;
        } else {
            graphics.drawString(font, "   §7Нет активного челленджа", col2X, contentY + 12, COLOR_MUTED);
        }

        // === КООРДИНАТЫ И ВРЕМЯ ===
        graphics.drawString(font, "§lПозиция", col2X, positionY, COLOR_TEXT);
        graphics.drawString(font, "   §7" + getPlayerCoords(), col2X, positionY + 12, COLOR_MUTED);
        graphics.drawString(font, "   " + getWorldTimeLabel(), col2X, positionY + 24, COLOR_MUTED);

        // === НИЖНЯЯ СЕКЦИЯ: Совет ===
        int adviceY = Math.max(top + 110, positionY + 44);
        graphics.hLine(left + 10, left + panelW - 10, adviceY - 5, 0xFF444466);
        graphics.drawString(font, "§l💡 Последний совет", left + 15, adviceY, COLOR_HEADER);

        String advice = getAdviceText();
        String[] lines = wrapText(advice, 50);
        int lineY = adviceY + 12;
        int adviceColor = getAdviceColor();
        for (String line : lines) {
            graphics.drawString(font, line, left + 20, lineY, adviceColor);
            lineY += 11;
            if (lineY > top + panelH - 45) break;
        }

        String timeStr = lastCompactData != null && lastCompactData.timestamp != null
                ? "Обновлено: " + lastCompactData.timestamp.substring(11, 16)
                : "Обновление...";
        graphics.drawString(font, "§7" + timeStr, left + 15, top + panelH - 50, COLOR_MUTED);
    }

    private void renderRecipesTab(GuiGraphics graphics, int left, int top, int panelW, int panelH) {
        graphics.drawString(font, "§l🧪 Поиск рецепта", left + 15, top + 30, COLOR_HEADER);

        // Пояснение
        graphics.drawString(font, "§7Введи предмет и нажми 'Найти'", left + 15, top + 62, COLOR_MUTED);

        // Результат
        int resultTop = top + 80;
        graphics.hLine(left + 10, left + panelW - 10, resultTop - 5, 0xFF444466);

        if (lastRecipeResponse == null) {
            graphics.drawString(font, "§7Рецепт пока не запрошен.", left + 15, resultTop + 10, COLOR_MUTED);
            return;
        }

        if (lastRecipeResponse.status == null || !"success".equalsIgnoreCase(lastRecipeResponse.status) || lastRecipeResponse.recipe == null) {
            String msg = lastRecipeResponse.message != null ? lastRecipeResponse.message : "Рецепт не найден.";
            String[] lines = wrapText(msg, 46);
            int y = resultTop + 10;
            for (String line : lines) {
                graphics.drawString(font, "§c" + line, left + 15, y, COLOR_HEALTH_LOW);
                y += 11;
                if (y > top + panelH - 60) break;
            }
            return;
        }

        String name = lastRecipeResponse.recipe.name != null ? lastRecipeResponse.recipe.name : "Рецепт";
        graphics.drawString(font, "§l" + name, left + 15, resultTop + 8, COLOR_TEXT);

        if (lastRecipeResponse.recipe.description != null && !lastRecipeResponse.recipe.description.isEmpty()) {
            String[] descLines = wrapText(lastRecipeResponse.recipe.description, 46);
            int y = resultTop + 20;
            for (String line : descLines) {
                graphics.drawString(font, "§7" + line, left + 15, y, COLOR_MUTED);
                y += 11;
                if (y > resultTop + 42) break;
            }
        }

        renderRecipeGrid(graphics, left + 15, resultTop + 38, lastRecipeResponse.recipe.grid);
    }

    private void renderRecipeGrid(GuiGraphics graphics, int x, int y, String[][] grid) {
        int cell = 28;
        int gap = 3;
        int gridW = cell * 3 + gap * 2;
        int gridH = cell * 3 + gap * 2;

        graphics.fill(x - 6, y - 6, x + gridW + 6, y + gridH + 6, 0x55222233);

        for (int row = 0; row < 3; row++) {
            for (int col = 0; col < 3; col++) {
                int cx = x + col * (cell + gap);
                int cy = y + row * (cell + gap);
                graphics.fill(cx, cy, cx + cell, cy + cell, 0xFF2a2a3a);
                graphics.hLine(cx, cx + cell, cy, 0xFF555566);
                graphics.hLine(cx, cx + cell, cy + cell - 1, 0xFF555566);
                graphics.vLine(cx, cy, cy + cell, 0xFF555566);
                graphics.vLine(cx + cell - 1, cy, cy + cell, 0xFF555566);

                String itemName = "";
                if (grid != null && row < grid.length && grid[row] != null && col < grid[row].length && grid[row][col] != null) {
                    itemName = grid[row][col];
                }
                itemName = itemName.trim();
                if (itemName.isEmpty() || "пусто".equalsIgnoreCase(itemName)) {
                    continue;
                }

                // Пробуем найти предмет в маппинге
                Item item = RECIPE_ITEM_MAP.get(itemName.toLowerCase());
                if (item == null) {
                    // Fallback: пробуем поиск по частичному совпадению
                    String lower = itemName.toLowerCase();
                    for (java.util.Map.Entry<String, Item> entry : RECIPE_ITEM_MAP.entrySet()) {
                        if (lower.contains(entry.getKey()) || entry.getKey().contains(lower)) {
                            item = entry.getValue();
                            break;
                        }
                    }
                }

                if (item != null) {
                    // Рисуем иконку предмета (16x16) по центру ячейки 28x28
                    ItemStack stack = new ItemStack(item);
                    graphics.renderItem(stack, cx + 6, cy + 6);
                } else {
                    // Fallback на короткий текст
                    String label = itemName.length() > 4 ? itemName.substring(0, 4) : itemName;
                    graphics.drawCenteredString(font, label, cx + cell / 2, cy + 10, 0xFFFFFFFF);
                }
            }
        }
    }

    // --- вкладка плана ---

    private String whyLabel() {
        return showWhy ? "§bДерево плана" : "§bПочему так";
    }

    private String modeLabel() {
        return "survival".equals(runtime.getAssistantMode()) ? "§aРежим: выживание" : "§eРежим: творчество";
    }

    private void refreshMode() {
        runtime.getHttpClient().getMode().thenAccept(response -> {
            if (response == null) {
                return;
            }
            Minecraft.getInstance().execute(() -> {
                runtime.setAssistantMode(response.mode);
                if (modeButton != null) {
                    modeButton.setMessage(Component.literal(modeLabel()));
                }
            });
        });
    }

    private void toggleMode() {
        String next = "survival".equals(runtime.getAssistantMode()) ? "creative" : "survival";
        runtime.getHttpClient().setMode(next).thenAccept(response -> {
            if (response == null || !response.ok) {
                return;
            }
            Minecraft.getInstance().execute(() -> {
                runtime.setAssistantMode(response.mode);
                if (modeButton != null) {
                    modeButton.setMessage(Component.literal(modeLabel()));
                }
            });
        });
    }

    /**
     * Строки для вкладки: дерево плана либо объяснение выбора между вариантами.
     */
    private java.util.List<String> planLines() {
        java.util.List<String> lines = new java.util.ArrayList<>();
        String text = showWhy ? runtime.getLastPlanAlternatives() : runtime.getLastPlanTree();
        if (text != null && !text.isBlank()) {
            lines.addAll(java.util.Arrays.asList(text.split("\\r?\\n")));
        }
        return lines;
    }

    private void renderPlanTab(GuiGraphics graphics, int left, int top, int panelW, int panelH) {
        String summary = runtime.getLastPlanSummary();
        graphics.drawString(font, showWhy ? "§l🔍 Почему план такой" : "§l🗺 План", left + 15, top + 34,
                COLOR_HEADER);

        int textLeft = left + 15;
        int maxWidth = panelW - 30;
        int y = top + 54;
        int bottom = top + panelH - 12;

        java.util.List<String> lines = planLines();
        if (lines.isEmpty()) {
            // Плана нет — показываем цепочку правил последнего решения, если она есть:
            // это объяснение отказа или выдачи.
            java.util.List<String> explanation = runtime.getLastExplanation();
            if (explanation == null || explanation.isEmpty()) {
                graphics.drawString(font, "§7Спроси в чате, например: «как сделать алмазную кирку».",
                        textLeft, y, COLOR_MUTED);
                return;
            }
            graphics.drawString(font, "§7Цепочка правил последнего решения:", textLeft, y, COLOR_MUTED);
            y += font.lineHeight + 4;
            for (String rule : explanation) {
                if (y > bottom) {
                    break;
                }
                graphics.drawString(font, font.plainSubstrByWidth("• " + rule, maxWidth), textLeft, y,
                        COLOR_TEXT);
                y += font.lineHeight + 2;
            }
            return;
        }

        if (!showWhy && summary != null && !summary.isBlank()) {
            graphics.drawString(font, font.plainSubstrByWidth("§7" + summary, maxWidth), textLeft, y,
                    COLOR_MUTED);
            y += font.lineHeight + 4;
        }

        int visible = Math.max(1, (bottom - y) / (font.lineHeight + 1));
        planScroll = Math.max(0, Math.min(planScroll, Math.max(0, lines.size() - visible)));
        for (int i = planScroll; i < lines.size() && i < planScroll + visible; i++) {
            String line = lines.get(i);
            // Длинные строки обрезаются: структура дерева и название предмета идут в начале
            // строки и остаются видны.
            String clipped = font.plainSubstrByWidth(line, maxWidth);
            if (clipped.length() < line.length()) {
                clipped = font.plainSubstrByWidth(line, maxWidth - font.width("…")) + "…";
            }
            int color = line.contains("— есть") ? COLOR_HEALTH_HIGH
                    : line.contains("не получено") ? COLOR_HEALTH_LOW : COLOR_TEXT;
            graphics.drawString(font, clipped, textLeft, y, color);
            y += font.lineHeight + 1;
        }
        if (lines.size() > visible) {
            graphics.drawString(font, "§7колесо мыши: " + (planScroll + 1) + "–"
                    + Math.min(lines.size(), planScroll + visible) + " из " + lines.size(),
                    left + panelW - 150, top + panelH - 11, COLOR_MUTED);
        }
    }

    @Override
    public boolean mouseScrolled(double mouseX, double mouseY, double delta) {
        if (activeTab == Tab.PLAN) {
            planScroll = Math.max(0, planScroll - (int) Math.signum(delta) * 3);
            return true;
        }
        return super.mouseScrolled(mouseX, mouseY, delta);
    }


    private String getPlayerCoords() {
        var player = Minecraft.getInstance().player;
        if (player == null) {
            return "—";
        }
        return String.format("X:%.0f Y:%.0f Z:%.0f", player.getX(), player.getY(), player.getZ());
    }

    private String getWorldTimeLabel() {
        var level = Minecraft.getInstance().level;
        if (level == null) {
            return "§7—";
        }
        long time = level.getDayTime() % 24000L;
        if (time < 12500) {
            return "§eДень";
        }
        if (time < 23000) {
            return "§9Ночь";
        }
        return "§6Рассвет";
    }

    private void searchRecipe() {
        if (recipeQueryBox == null) {
            return;
        }
        String query = recipeQueryBox.getValue();
        lastRecipeQuery = query;
        lastRecipeResponse = null;

        runtime.getHttpClient().getCraftRecipe(query).thenAccept(resp -> {
            Minecraft.getInstance().execute(() -> {
                lastRecipeResponse = resp;
            });
        });
    }
    
    private void renderHealthBar(GuiGraphics graphics, int x, int y, Double health) {
        int barW = 140;
        int barH = 10;
        
        // Фон полоски
        graphics.fill(x, y, x + barW, y + barH, 0xFF333344);
        
        if (health != null) {
            float pct = Math.min(1.0f, Math.max(0.0f, health.floatValue() / 20.0f));
            int filled = (int) (barW * pct);
            
            int color = pct > 0.6f ? COLOR_HEALTH_HIGH : pct > 0.3f ? COLOR_HEALTH_MED : COLOR_HEALTH_LOW;
            graphics.fill(x, y, x + filled, y + barH, color);
            
            // Текст значения
            String text = String.format("%.1f/20", health);
            graphics.drawCenteredString(font, text, x + barW / 2, y + 1, 0xFFFFFFFF);
        } else {
            graphics.drawCenteredString(font, "—", x + barW / 2, y + 1, COLOR_MUTED);
        }
        
        // Рамка
        graphics.hLine(x, x + barW, y, 0xFF555566);
        graphics.hLine(x, x + barW, y + barH - 1, 0xFF555566);
        graphics.vLine(x, y, y + barH, 0xFF555566);
        graphics.vLine(x + barW - 1, y, y + barH, 0xFF555566);
    }
    
    private String getAdviceText() {
        if (lastCompactData != null && lastCompactData.advice != null && !lastCompactData.advice.isEmpty()) {
            return lastCompactData.advice;
        }
        String cached = runtime.getLastAdvice();
        if (cached != null && !cached.isEmpty()) {
            return cached;
        }
        return "Нажмите §b🔄 Обновить§r для получения советов...";
    }
    
    private int getAdviceColor() {
        if (lastCompactData != null && lastCompactData.advice_level != null) {
            return switch (lastCompactData.advice_level) {
                case "CRITICAL" -> COLOR_HEALTH_LOW;
                case "WARNING" -> COLOR_HEALTH_MED;
                default -> COLOR_TEXT;
            };
        }
        return COLOR_TEXT;
    }
    
    private void fetchCompactData() {
        runtime.getHttpClient().getCompactStatus().thenAccept(data -> {
            Minecraft.getInstance().execute(() -> {
                lastCompactData = data;
                lastDataFetchTime = System.currentTimeMillis();
            });
        });
    }
    
    @Override
    public boolean keyPressed(int keyCode, int scanCode, int modifiers) {
        if (keyCode == 256) { // ESC
            onClose();
            return true;
        }

        if (activeTab == Tab.STATUS) {
            if (keyCode == 79) { // O - обновление данных статуса
                fetchCompactData();
                return true;
            }
        } else {
            // Если фокус в поле ввода — отдаём ввод EditBox'у (чтобы можно было печатать букву "o")
            boolean editBoxFocused = recipeQueryBox != null && recipeQueryBox.isFocused();
            if (!editBoxFocused && keyCode == 79) { // O — поиск, только когда поле НЕ в фокусе
                searchRecipe();
                return true;
            }
            if (keyCode == 257 || keyCode == 335) { // ENTER / KP_ENTER — поиск всегда
                searchRecipe();
                return true;
            }
        }
        return super.keyPressed(keyCode, scanCode, modifiers);
    }
    
    @Override
    public boolean isPauseScreen() {
        return false;
    }
    
    @Override
    public void tick() {
        super.tick();
        if (activeTab == Tab.STATUS) {
            // Автообновление каждые 5 секунд если окно открыто
            if (System.currentTimeMillis() - lastDataFetchTime > 5000) {
                fetchCompactData();
            }
        }
    }
    
    private String[] wrapText(String text, int maxLen) {
        if (text.length() <= maxLen) {
            return new String[] { text };
        }
        
        java.util.List<String> lines = new java.util.ArrayList<>();
        StringBuilder current = new StringBuilder();
        
        for (String word : text.split(" ")) {
            if (current.length() + word.length() + 1 > maxLen) {
                lines.add(current.toString());
                current = new StringBuilder();
            }
            if (current.length() > 0) {
                current.append(" ");
            }
            current.append(word);
        }
        
        if (current.length() > 0) {
            lines.add(current.toString());
        }
        
        return lines.toArray(new String[0]);
    }
}

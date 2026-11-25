package com.example;

import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.keybinding.v1.KeyBindingHelper;
import net.fabricmc.fabric.api.client.message.v1.ClientSendMessageEvents;
import net.minecraft.client.KeyMapping;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.screens.Screen;

import com.example.assistant.AssistantConfig;
import com.example.assistant.AssistantOverlayScreen;
import com.example.assistant.AssistantRuntime;
import com.example.assistant.ConfigManager;
import org.lwjgl.glfw.GLFW;

public class ExampleModClient implements ClientModInitializer {
	private static AssistantRuntime runtime;
	private static KeyMapping overlayKey;
	private static KeyMapping autopilotStopKey;

	@Override
	public void onInitializeClient() {
		AssistantConfig cfg = ConfigManager.loadOrCreate();
		runtime = new AssistantRuntime(cfg);

		// Регистрация горячей клавиши для оверлея (клавиша O)
		overlayKey = KeyBindingHelper.registerKeyBinding(new KeyMapping(
			"key.aiassistant.overlay",
			GLFW.GLFW_KEY_O,
			"category.aiassistant.general"
		));

		// «Сделай сам»: остановка автопилота (клавиша K)
		autopilotStopKey = KeyBindingHelper.registerKeyBinding(new KeyMapping(
			"key.aiassistant.autopilot_stop",
			GLFW.GLFW_KEY_K,
			"category.aiassistant.general"
		));

		ClientTickEvents.END_CLIENT_TICK.register(client -> {
			// Основной тик ассистента
			runtime.onClientTick(client);
			
			while (autopilotStopKey.consumeClick()) {
				runtime.getAutopilot().stopByPlayer(client);
			}

			// Проверка нажатия клавиши оверлея
			while (overlayKey.consumeClick()) {
				if (client.screen == null) {
					client.setScreen(new AssistantOverlayScreen(runtime));
				}
			}
		});

		ClientSendMessageEvents.CHAT.register((message) -> {
			if (runtime != null) {
				System.out.println("[AI Assistant] Player sent chat: " + message);
				runtime.sendChatMessage(message);
			}
		});
	}

	public static AssistantRuntime getRuntime() {
		return runtime;
	}
}
package com.example.assistant;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

import javax.sound.sampled.AudioFormat;
import javax.sound.sampled.AudioSystem;
import javax.sound.sampled.DataLine;
import javax.sound.sampled.TargetDataLine;

import net.minecraft.client.Minecraft;
import net.minecraft.network.chat.Component;

/**
 * Голос в игре: пока клавиша зажата, пишется микрофон; при отпускании запись уходит на
 * сервер, а распознанный текст — тем же путём, что набранная в чате реплика.
 *
 * Голос — способ ввести реплику. Команд игре он не отдаёт: текст проходит тот же конвейер
 * и тот же валидатор, что и набранный.
 */
public final class VoiceInput {
    private static final float RATE = 16000f;
    // Запись дольше не отправляется: реплика игрока — секунды (предел сервера тот же).
    private static final int MAX_BYTES = (int) (RATE * 2 * 30);
    // Короче — случайное касание клавиши, а не реплика.
    private static final int MIN_BYTES = (int) (RATE * 2 * 0.3);

    private final AssistantRuntime runtime;
    private TargetDataLine line;
    private Thread reader;
    private ByteArrayOutputStream buffer;
    private volatile boolean recording;
    private boolean wasDown;

    public VoiceInput(AssistantRuntime runtime) {
        this.runtime = runtime;
    }

    /** Вызывается каждый тик с состоянием клавиши: начало и конец записи — по её перепадам. */
    public void onTick(Minecraft client, boolean down) {
        if (down && !wasDown) {
            start(client);
        } else if (!down && wasDown) {
            finish(client);
        }
        wasDown = down;
    }

    private void start(Minecraft client) {
        if (recording || client.player == null) {
            return;
        }
        AudioFormat format = new AudioFormat(RATE, 16, 1, true, false);
        try {
            TargetDataLine opened = (TargetDataLine) AudioSystem.getLine(
                    new DataLine.Info(TargetDataLine.class, format));
            opened.open(format);
            opened.start();
            line = opened;
        } catch (Exception error) {
            hint(client, "§c[AI] Микрофон недоступен: " + error.getClass().getSimpleName());
            line = null;
            return;
        }
        buffer = new ByteArrayOutputStream();
        recording = true;
        final TargetDataLine source = line;
        final ByteArrayOutputStream target = buffer;
        reader = new Thread(() -> {
            byte[] chunk = new byte[3200];
            while (recording && target.size() < MAX_BYTES) {
                int read = source.read(chunk, 0, chunk.length);
                if (read > 0) {
                    target.write(chunk, 0, read);
                }
            }
        }, "ai-assistant-voice");
        reader.setDaemon(true);
        reader.start();
        hint(client, "§a● Говори — отпусти клавишу, когда закончишь");
    }

    private void finish(Minecraft client) {
        if (!recording) {
            return;
        }
        recording = false;
        try {
            line.stop();
            line.close();
            reader.join(500);
        } catch (Exception ignored) {
            // запись уже остановлена; собранное всё равно годится
        }
        byte[] pcm = buffer.toByteArray();
        line = null;
        if (pcm.length < MIN_BYTES) {
            hint(client, "§7Слишком коротко — держи клавишу, пока говоришь");
            return;
        }
        hint(client, "§7Распознаю…");
        runtime.getHttpClient().transcribe(wav(pcm)).thenAccept(text -> client.execute(() -> {
            if (client.player == null) {
                return;
            }
            if (text == null || text.isBlank()) {
                hint(client, "§7Не расслышал — повтори");
                return;
            }
            // Реплика показывается игроку: он видит, что именно услышано, до ответа.
            client.player.displayClientMessage(Component.literal("§b[Голос] §f" + text), false);
            runtime.sendChatMessage(text);
        }));
    }

    private static void hint(Minecraft client, String text) {
        if (client.player != null) {
            client.player.displayClientMessage(Component.literal(text), true);
        }
    }

    /** Заголовок WAV перед сырыми отсчётами: 16 бит, моно, 16 кГц. */
    static byte[] wav(byte[] pcm) {
        ByteBuffer out = ByteBuffer.allocate(44 + pcm.length).order(ByteOrder.LITTLE_ENDIAN);
        out.put(new byte[] {'R', 'I', 'F', 'F'}).putInt(36 + pcm.length);
        out.put(new byte[] {'W', 'A', 'V', 'E', 'f', 'm', 't', ' '}).putInt(16);
        out.putShort((short) 1).putShort((short) 1).putInt((int) RATE).putInt((int) RATE * 2);
        out.putShort((short) 2).putShort((short) 16);
        out.put(new byte[] {'d', 'a', 't', 'a'}).putInt(pcm.length).put(pcm);
        return out.array();
    }
}

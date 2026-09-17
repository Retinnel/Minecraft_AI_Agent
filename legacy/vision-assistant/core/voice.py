import threading
import time
import numpy as np
import pyaudio
from faster_whisper import WhisperModel
from backend.core.config import settings

class VoiceListener:
    def __init__(self, model_name=None, device=None, threshold=0.02, silence_duration=0.5):
        self.model_name = model_name or settings.whisper_model
        self.device = device or settings.whisper_device
        self.model = None
        self.model_loaded = False
        
        self.threshold = threshold
        self.silence_duration = silence_duration
        self.audio = pyaudio.PyAudio()
        self.stream = None
        self.is_listening = False
        self.on_transcription = None
        
        # Загружаем модель в фоновом потоке, чтобы не блокировать запуск программы
        threading.Thread(target=self._init_model, daemon=True).start()

    def _init_model(self):
        print("[INFO] Начинается фоновая загрузка голосовой модели (faster-whisper)...")
        # 1. Смена модели на "base" (в 3 раза быстрее small)
        # 2. compute_type="int8" (критично для CPU, ускоряет в 2-3 раза)
        # 3. cpu_threads=4 (используем больше ядер)
        self.model = WhisperModel(
            self.model_name, 
            device=self.device, 
            compute_type="int8", 
            cpu_threads=4, 
            num_workers=2
        )
        self.model_loaded = True
        print("[SUCCESS] Голосовая модель (faster-whisper) успешно загружена и готова к работе!")

    def start(self):
        if self.is_listening:
            return
        try:
            self.stream = self.audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=16000,
                input=True,
                frames_per_buffer=1024
            )
            self.is_listening = True
            self.recording_buffer = []
            self.is_recording = False
            print("[INFO] Микрофон активирован.")
            # Фоновый поток теперь просто читает данные в буфер, если идет запись
            threading.Thread(target=self._read_loop, daemon=True).start()
        except Exception as e:
            print(f"[ERROR] Не удалось активировать микрофон: {e}")
            self.is_listening = False

    def start_recording(self):
        if not self.is_recording:
            self.is_recording = True
            self.recording_buffer = []
            print("[INFO] Запись голоса начата...")

    def stop_recording(self):
        if self.is_recording:
            self.is_recording = False
            if len(self.recording_buffer) > 4000:
                audio_np = np.array(self.recording_buffer)
                self._process_audio(audio_np)
            self.recording_buffer = []

    def _read_loop(self):
        while self.is_listening:
            try:
                data = self.stream.read(1024, exception_on_overflow=False)
                if self.is_recording:
                    audio_data = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                    self.recording_buffer.extend(audio_data)
            except Exception as e:
                print(f"[ERROR] Audio read error: {e}")
                break

    def _process_audio(self, audio_np):
        if not self.model_loaded:
            print("[WAIT] Модель всё ещё загружается...")
            return
        
        try:
            segments, _ = self.model.transcribe(
                audio_np, 
                language="ru", 
                beam_size=5,        # Исправлено на 1 для скорости
                best_of=1,
                vad_filter=True,
                initial_prompt="Игровой помощник. Кратко."
            )
            
            text = " ".join([seg.text for seg in segments]).strip()
            if text and self.on_transcription:
                self.on_transcription(text)
        except Exception as e:
            print(f"[ERROR] Transcription failed: {e}")

    def stop(self):
        self.is_listening = False
        if self.stream:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        print("[INFO] Микрофон остановлен.")

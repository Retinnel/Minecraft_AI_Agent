from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache

class Settings(BaseSettings):
    # Gemini API Key
    gemini_api_key: str 
    
    # Whisper (Voice Transcription) Settings
    whisper_model: str
    whisper_device: str
    
    # TTS (Text to Speech) Settings
    tts_model_id: str
    tts_speaker: str
    tts_sample_rate: int
    tts_speed: float

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

@lru_cache()
def get_settings():
    return Settings()

settings = get_settings()

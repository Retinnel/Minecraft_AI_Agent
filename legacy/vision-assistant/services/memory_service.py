import os
import time

import chromadb
from chromadb.utils import embedding_functions

from backend.core.config import settings

# Максимальное количество сообщений, хранимых в ChromaDB.
# Предотвращает раздувание промпта при длинных сессиях (Truncation по ТЗ).
_MAX_COLLECTION_SIZE = 100


class MemoryService:
    """
    Сервис долговременной памяти на базе ChromaDB.
    Хранит историю диалога и реализует RAG для контекстного поиска.
    """

    def __init__(self):
        db_path = os.path.join(os.getcwd(), "data", "chroma")
        os.makedirs(db_path, exist_ok=True)

        self.client = chromadb.PersistentClient(path=db_path)

        # Локальные эмбеддинги (Sentence-Transformers) — без отправки данных на внешние серверы
        embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )

        self.collection = self.client.get_or_create_collection(
            name="dialogue_history",
            embedding_function=embedding_fn,
            metadata={"hnsw:space": "cosine"},
        )

    def add_message(self, role: str, content: str) -> None:
        """Добавляет сообщение в историю. При превышении лимита удаляет старейшие записи."""
        self._truncate_if_needed()

        msg_id = f"{role}_{time.time()}"
        self.collection.add(
            ids=[msg_id],
            documents=[content],
            metadatas=[{"role": role, "timestamp": time.time()}],
        )

    def get_context(self, query: str, n_results: int = 5) -> str:
        """Возвращает семантически близкие фрагменты из истории диалога."""
        # ChromaDB выбрасывает исключение, если коллекция пуста или n_results > кол-во записей
        count = self.collection.count()
        if count == 0:
            return ""

        actual_n = min(n_results, count)
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=actual_n,
            )
            if results and results["documents"]:
                return "\n".join(results["documents"][0])
        except Exception as e:
            print(f"[ERROR] Memory query failed: {e}")

        return ""

    def get_recent_history(self, limit: int = 10) -> list:
        """Возвращает последние N сообщений в хронологическом порядке."""
        count = self.collection.count()
        if count == 0:
            return []
        
        # Получаем все сообщения, сортируем по времени и берем последние
        all_data = self.collection.get(include=["metadatas", "documents"])
        items = []
        for i in range(len(all_data["ids"])):
            items.append({
                "role": all_data["metadatas"][i]["role"],
                "content": all_data["documents"][i],
                "timestamp": all_data["metadatas"][i]["timestamp"]
            })
        
        # Сортировка по времени (от старых к новым)
        sorted_items = sorted(items, key=lambda x: x["timestamp"])
        return sorted_items[-limit:]

    def clear_session(self) -> None:
        """Полностью очищает историю диалога."""
        try:
            self.client.delete_collection("dialogue_history")
        except:
            pass
        
        # Пересоздаём коллекцию с функцией эмбеддингов
        embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        self.collection = self.client.get_or_create_collection(
            name="dialogue_history",
            embedding_function=embedding_fn,
            metadata={"hnsw:space": "cosine"},
        )
        print("[INFO] Memory cleared and session restarted.")

    def _truncate_if_needed(self) -> None:
        """Удаляет самые старые записи, если превышен лимит _MAX_COLLECTION_SIZE."""
        count = self.collection.count()
        if count < _MAX_COLLECTION_SIZE:
            return

        # Получаем все записи с метаданными, сортируем по timestamp, удаляем старейшие
        overflow = count - _MAX_COLLECTION_SIZE + 1
        all_items = self.collection.get(include=["metadatas"])
        if not all_items or not all_items["ids"]:
            return

        # Сортируем по timestamp и берём самые старые
        items_with_ts = sorted(
            zip(all_items["ids"], all_items["metadatas"]),
            key=lambda x: x[1].get("timestamp", 0),
        )
        ids_to_delete = [item_id for item_id, _ in items_with_ts[:overflow]]
        self.collection.delete(ids=ids_to_delete)

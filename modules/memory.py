from modules.module import Module
from constants import *
from chromadb.config import Settings
import chromadb
import requests
import json
import uuid
import asyncio
import copy
import os

class Memory(Module):

    def __init__(self, signals, enabled=True):
        super().__init__(signals, enabled)

        self.API = self.API(self)
        self.prompt_injection.text = ""
        self.prompt_injection.priority = 60

        self.processed_count = 0

        self.chroma_client = chromadb.PersistentClient(path="./memories/chroma.db", settings=Settings(anonymized_telemetry=False))
        self.collection = self.chroma_client.get_or_create_collection(name="neuro_collection")
        print(f"MEMORY: Loaded {self.collection.count()} memories from database.")
        if self.collection.count() == 0:
            print("MEMORY: No memories found in database. Importing from memoryinit.json")
            self.API.import_json(path="./memories/memoryinit.json")

    def clear_database(self):
        """Bridge method called by prompter to wipe ChromaDB collection."""
        self.API.wipe()

    def get_prompt_injection(self):
        query = ""

        # Query using local history context instead of Twitch queues
        for message in self.signals.history[-MEMORY_QUERY_MESSAGE_COUNT:]:
            if message.get("role") == "user" and message.get("content"):
                query += HOST_NAME + ": " + str(message["content"]) + "\n"
            elif message.get("role") == "assistant" and message.get("content"):
                query += AI_NAME + ": " + str(message["content"]) + "\n"

        if not query.strip():
            self.prompt_injection.text = ""
            return self.prompt_injection

        memories = self.collection.query(query_texts=query, n_results=MEMORY_RECALL_COUNT)

        self.prompt_injection.text = f"{AI_NAME} remembers these relevant details:\n"
        for i in range(len(memories["ids"][0])):
            self.prompt_injection.text += memories['documents'][0][i] + "\n"
        self.prompt_injection.text += "End of memory recall.\n"

        return self.prompt_injection

    async def run(self):
        while not self.signals.terminate:
            if self.processed_count > len(self.signals.history):
                self.processed_count = 0

            if len(self.signals.history) - self.processed_count >= 10:
                if getattr(self.signals, 'new_message', False) or getattr(self.signals, 'AI_thinking', False):
                    # Skip or delay reflection for this cycle so it doesn't collide with typing
                    await asyncio.sleep(2)
                    continue
                
                # print("MEMORY: Performing background reflection...")

                messages = copy.deepcopy(self.signals.history[-(len(self.signals.history) - self.processed_count):])

                chat_section = ""
                for message in messages:
                    role = HOST_NAME if message.get("role") == "user" else AI_NAME
                    chat_section += f"{role}: {message.get('content', '')}\n"

                data = {
                    "model": MODEL_NAME,
                    "messages": [{
                        "role": "user",
                        "content": chat_section + MEMORY_PROMPT
                    }],
                    "max_tokens": 200
                }
                headers = {"Content-Type": "application/json"}

                try:
                    response = requests.post(f"{LLM_ENDPOINT}/v1/chat/completions", headers=headers, json=data, timeout=45)
                    if response.status_code == 200:
                        raw_memories = response.json()['choices'][0]['message']['content']
                        for memory in raw_memories.split("{qa}"):
                            memory = memory.strip()
                            if memory:
                                self.collection.upsert([str(uuid.uuid4())], documents=[memory], metadatas=[{"type": "short-term"}])
                except requests.exceptions.Timeout:
                    print("MEMORY: Reflection query timed out. The model might be busy. Skipping for this turn.")
                except Exception as e:
                    print(f"MEMORY: Error during reflection query: {e}")

                self.processed_count = len(self.signals.history)

            await asyncio.sleep(5)

    class API:
        def __init__(self, outer):
            self.outer = outer

        def create_memory(self, data):
            self.outer.collection.upsert(str(uuid.uuid4()), documents=data, metadatas={"type": "short-term"})

        def delete_memory(self, id):
            self.outer.collection.delete(id)

        def wipe(self):
            """Safely clears the database by deleting and recreating the Chroma collection."""
            try:
                self.outer.chroma_client.delete_collection(name="neuro_collection")
            except Exception:
                pass
            self.outer.collection = self.outer.chroma_client.get_or_create_collection(name="neuro_collection")
            print("MEMORY: Database successfully wiped and reset.")

        def import_json(self, path="./memories/memoryinit.json"):
            if not os.path.exists(path):
                print(f"MEMORY: No initialization file found at {path}. Starting with empty collection.")
                return

            with open(path, "r", encoding="utf-8") as file:
                try:
                    data = json.load(file)
                except json.JSONDecodeError:
                    return

            for memory in data.get("memories", []):
                self.outer.collection.upsert(memory["id"], documents=memory["document"], metadatas=memory["metadata"])
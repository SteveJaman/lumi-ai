import os
import pypdf
from docx import Document
from constants import WORKSPACE_ROOT

class DocumentManager:
    def __init__(self, library_path=None):
        if library_path is None:
            library_path = os.path.join(WORKSPACE_ROOT, "library")
        self.library_path = library_path
        os.makedirs(self.library_path, exist_ok=True)

    def scan_and_extract(self, filename):
        file_path = os.path.join(self.library_path, filename)
        if not os.path.exists(file_path):
            return f"Error: Document '{filename}' not found in library."

        ext = os.path.splitext(file_path)[1].lower()
        full_text = ""

        try:
            if ext == ".pdf":
                reader = pypdf.PdfReader(file_path)
                for i, page in enumerate(reader.pages):
                    txt = page.extract_text()
                    if txt:
                        full_text += f"\n[Page {i+1}]\n{txt}"
            elif ext in [".docx", ".doc"]:
                doc = Document(file_path)
                full_text = "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
            elif ext in [".txt", ".md", ".xml", ".json"]:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    full_text = f.read()
            else:
                return f"Error: Unsupported file format {ext}"

            return full_text
        except Exception as e:
            return f"Error reading document: {e}"
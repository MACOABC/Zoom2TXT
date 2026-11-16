"""Unit and integration tests for the Local AI Study Assistant (RAG Engine, SQLite FTS5,
Document Ingestion, Strict Scope Isolation, and PySide6 AssistantView)."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import numpy as np
import test_hardening as base

from zoom2txt.blackboard.registry import Registry
from zoom2txt.config import (
    load_gemini_api_key,
    save_gemini_api_key,
)
from zoom2txt.gui.assistant_view import AssistantView
from zoom2txt.gui.health_view import HealthView
from zoom2txt.gui.main_window import MainWindow
from zoom2txt.rag_engine import (
    NO_INFO_RESPONSE,
    STRICT_SYSTEM_PROMPT,
    RAGEngine,
    RateLimitExceededError,
    chunk_text,
    cosine_similarity,
    deserialize_embedding,
    extract_sections_from_file,
    ingest_file,
    serialize_embedding,
    sync_existing_transcripts,
)
from zoom2txt.security import redact
from zoom2txt.storage import StateStore, atomic_write


class RagAssistantTests(unittest.TestCase):
    setUp = base.HardeningTests.setUp
    tearDown = base.HardeningTests.tearDown

    def test_wal_mode_fts5_tables_and_lossless_migration(self):
        """Verifica migración sin pérdida de datos, modo WAL, busy_timeout=30000 y FTS5 sincronizado."""
        db_path = self.root / "legacy_state.sqlite3"
        # Simular una base de datos de versión anterior con solo la tabla records
        with closing(sqlite3.connect(db_path)) as old_db, old_db:
            old_db.execute(
                "CREATE TABLE records (namespace TEXT, kind TEXT, id TEXT, payload TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(namespace,kind,id))"
            )
            old_db.execute(
                "INSERT INTO records VALUES ('ns1', 'job', 'j1', '{\"status\":\"done\"}', 100.0)"
            )

        store = StateStore(self.s, path=db_path)
        with store.connect() as db:
            journal = db.execute("PRAGMA journal_mode").fetchone()[0].lower()
            busy = db.execute("PRAGMA busy_timeout").fetchone()[0]
            fk = db.execute("PRAGMA foreign_keys").fetchone()[0]
            self.assertEqual(journal, "wal")
            self.assertEqual(busy, 30000)
            self.assertEqual(fk, 1)

            # Verificar que los datos previos en records siguen intactos
            old_row = db.execute(
                "SELECT payload FROM records WHERE namespace='ns1' AND kind='job' AND id='j1'"
            ).fetchone()
            self.assertIsNotNone(old_row)

            # Verificar que las tablas nuevas existen
            tables = {
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
            for expected in (
                "records",
                "courses",
                "knowledge_sources",
                "knowledge_chunks",
                "knowledge_chunks_fts",
                "chat_sessions",
                "chat_messages",
            ):
                self.assertIn(expected, tables)

            # Verificar ON DELETE CASCADE y sincronización con FTS5
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute(
                """
                INSERT INTO knowledge_sources
                    (namespace, course_id, course_name, source_type, title, file_path, file_hash, is_active, created_at)
                VALUES (?, 'c1', 'Curso 1', 'transcript', 'Clase 1', '/tmp/c1.txt', 'h1', 1, '2026-10-27')
                """,
                (store.namespace,),
            )
            src_id = cur.lastrowid
            db.execute(
                """
                INSERT INTO knowledge_chunks (source_id, course_id, chunk_index, content, metadata_json)
                VALUES (?, 'c1', 0, 'Arquitectura de microservicios con eventos asíncronos', '{}')
                """,
                (src_id,),
            )

            fts_hit = db.execute(
                "SELECT chunk_id FROM knowledge_chunks_fts WHERE knowledge_chunks_fts MATCH '\"microservicios\"'"
            ).fetchall()
            self.assertEqual(len(fts_hit), 1)

            # Al borrar la fuente, ON DELETE CASCADE y el trigger deben limpiar chunks y FTS5
            db.execute("DELETE FROM knowledge_sources WHERE id = ?", (src_id,))
            rem_chunks = db.execute(
                "SELECT COUNT(*) FROM knowledge_chunks WHERE source_id = ?", (src_id,)
            ).fetchone()[0]
            rem_fts = db.execute(
                "SELECT COUNT(*) FROM knowledge_chunks_fts WHERE knowledge_chunks_fts MATCH '\"microservicios\"'"
            ).fetchone()[0]
            self.assertEqual(rem_chunks, 0)
            self.assertEqual(rem_fts, 0)

    def test_smart_text_chunker_bounds_and_overlap(self):
        """Verifica que el Text Chunker genere bloques de ~600-800 palabras con solapamiento de 100 palabras."""
        paragraphs = []
        for p_idx in range(12):
            words = [f"palabra_{p_idx}_{w}" for w in range(180)]
            paragraphs.append(" ".join(words))
        full_text = "\n\n".join(paragraphs)  # 2160 palabras en 12 párrafos

        chunks = chunk_text(
            full_text,
            target_words=650,
            max_words=800,
            overlap_words=100,
            is_transcript=True,
        )
        self.assertGreaterEqual(len(chunks), 3)
        for ch in chunks[:-1]:
            w_count = len(ch.content.split())
            self.assertGreaterEqual(w_count, 600)
            self.assertLessEqual(w_count, 800)
            self.assertIn("approx_minute", ch.metadata)

        # Verificar que las últimas 100 palabras del primer bloque están al inicio del segundo bloque
        first_tail = chunks[0].content.split()[-100:]
        second_head = chunks[1].content.split()[:100]
        self.assertEqual(first_tail, second_head)

    def test_multi_format_material_extraction_and_hash_deduplication(self):
        """Verifica la ingesta de transcripciones y materiales (.pdf, .pptx, .docx, .txt) y SHA-256."""
        from docx import Document
        from pptx import Presentation
        from pypdf import PdfWriter

        # 1. TXT / Transcripción
        txt_path = self.s.transcripts_dir / "IngenieriaSoftware_Semana01_Para_NotebookLM.txt"
        atomic_write(
            txt_path,
            "El patrón Repository desacopla la lógica de dominio del acceso a datos SQLite.",
        )
        sid1 = ingest_file(
            self.s,
            txt_path,
            course_id="ing-soft",
            course_name="Ingeniería de Software",
            source_type="transcript",
        )
        self.assertIsNotNone(sid1)

        # Re-ingestar sin cambios no debe duplicar ni recrear chunks
        sid1_again = ingest_file(
            self.s,
            txt_path,
            course_id="ing-soft",
            course_name="Ingeniería de Software",
            source_type="transcript",
        )
        self.assertEqual(sid1, sid1_again)

        # 2. PPTX
        pptx_path = self.s.materials_dir / "Diapositivas_Semana01.pptx"
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[0])
        slide.shapes.title.text = " Principios SOLID "
        slide.placeholders[1].text = "Inversión de dependencias y responsabilidad única."
        prs.save(str(pptx_path))
        pptx_sections = extract_sections_from_file(pptx_path)
        self.assertEqual(len(pptx_sections), 1)
        self.assertEqual(pptx_sections[0].metadata["slide"], 1)
        self.assertIn("SOLID", pptx_sections[0].text)
        sid_pptx = ingest_file(
            self.s,
            pptx_path,
            course_id="ing-soft",
            course_name="Ingeniería de Software",
        )
        self.assertIsNotNone(sid_pptx)

        # 3. DOCX
        docx_path = self.s.materials_dir / "Silabo_Curso.docx"
        doc = Document()
        doc.add_heading("Evaluación Parcial", level=1)
        doc.add_paragraph("El examen parcial abarca hasta la semana 7 del curso.")
        doc.save(str(docx_path))
        docx_sections = extract_sections_from_file(docx_path)
        self.assertEqual(len(docx_sections), 1)
        self.assertEqual(docx_sections[0].metadata.get("section"), "Evaluación Parcial")
        sid_docx = ingest_file(
            self.s,
            docx_path,
            course_id="ing-soft",
            course_name="Ingeniería de Software",
        )
        self.assertIsNotNone(sid_docx)

        # 4. PDF válido
        pdf_path = self.s.materials_dir / "Lectura.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with pdf_path.open("wb") as f:
            writer.write(f)
        # Página en blanco devuelve 0 secciones sin lanzar excepción
        self.assertEqual(extract_sections_from_file(pdf_path), [])

    def test_auto_ingest_hook_and_sync_existing_transcripts(self):
        """Verifica que Registry.mark_done auto-indexe la transcripción y sync_existing_transcripts detecte archivos previos."""
        t1 = self.s.transcripts_dir / "RedesComputadoras_Semana02_Para_NotebookLM.txt"
        atomic_write(t1, "En esta sesión explicamos el protocolo TCP y el control de congestión.")
        Registry(self.s).mark_done("redes-101", "meet-1", "RedesComputadoras_Semana02", t1)

        store = StateStore(self.s)
        sources = store.list_knowledge_sources(course_id="redes-101")
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["source_type"], "transcript")

        # Añadir otra transcripción antigua directamente en disco y correr sync_existing_transcripts
        t2 = self.s.transcripts_dir / "BaseDeDatos_Semana04_Para_NotebookLM.txt"
        atomic_write(t2, "Los índices B-Tree aceleran las consultas por rango en SQLite.")
        stats = sync_existing_transcripts(self.s)
        self.assertGreaterEqual(stats["indexed"], 2)

        all_sources = store.list_knowledge_sources(all_courses=True)
        self.assertEqual(len(all_sources), 2)

    def test_strict_single_course_filtering_versus_all_courses_mode(self):
        """Verifica el aislamiento estricto por course_id frente al modo global (all_courses=True)
        y la activación/desactivación de fuentes."""
        c1_txt = self.s.transcripts_dir / "CursoIA_Clase1_Para_NotebookLM.txt"
        c2_txt = self.s.transcripts_dir / "CursoSeguridad_Clase1_Para_NotebookLM.txt"
        atomic_write(
            c1_txt,
            "Las redes neuronales transformadoras utilizan mecanismos de atención multicabeza.",
        )
        atomic_write(
            c2_txt,
            "El cifrado autenticado AES-GCM protege la confidencialidad e integridad de los paquetes.",
        )

        sid1 = ingest_file(
            self.s,
            c1_txt,
            course_id="curso-ia",
            course_name="Inteligencia Artificial",
            source_type="transcript",
        )
        sid2 = ingest_file(
            self.s,
            c2_txt,
            course_id="curso-seg",
            course_name="Seguridad Informática",
            source_type="transcript",
        )
        self.assertIsNotNone(sid1)
        self.assertIsNotNone(sid2)

        engine = RAGEngine(self.s)

        # 1. Consulta filtrada estrictamente a 'curso-ia': nunca debe devolver chunks de 'curso-seg'
        chunks_ia, _ = engine.retrieve(
            "atención multicabeza o cifrado AES-GCM",
            course_id="curso-ia",
            all_courses=False,
            use_embeddings=False,
        )
        self.assertTrue(chunks_ia)
        self.assertEqual({c.course_id for c in chunks_ia}, {"curso-ia"})

        # 2. Consulta filtrada estrictamente a 'curso-seg': nunca debe devolver chunks de 'curso-ia'
        chunks_seg, _ = engine.retrieve(
            "atención multicabeza o cifrado AES-GCM",
            course_id="curso-seg",
            all_courses=False,
            use_embeddings=False,
        )
        self.assertTrue(chunks_seg)
        self.assertEqual({c.course_id for c in chunks_seg}, {"curso-seg"})

        # 3. Modo Global (all_courses=True): recupera fuentes compartidas de ambos cursos simultáneamente
        chunks_global, _ = engine.retrieve(
            "atención multicabeza cifrado AES-GCM",
            course_id=None,
            all_courses=True,
            use_embeddings=False,
        )
        self.assertEqual({c.course_id for c in chunks_global}, {"curso-ia", "curso-seg"})

        # 4. Si se desmarca una fuente en la UI (is_active=False), queda excluida
        engine.store.set_source_active(sid2, False)
        chunks_after_disable, _ = engine.retrieve(
            "atención multicabeza cifrado AES-GCM",
            course_id=None,
            all_courses=True,
            use_embeddings=False,
        )
        self.assertEqual({c.course_id for c in chunks_after_disable}, {"curso-ia"})

    def test_hybrid_embeddings_and_model_fallback_on_429(self):
        """Verifica serialización float32, similitud coseno, fallback de modelos ante 429
        y cumplimiento del Strict Grounding System Prompt."""
        v1 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        v2 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        v3 = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        blob = serialize_embedding(v1)
        restored = deserialize_embedding(blob)
        self.assertTrue(np.allclose(v1, restored))
        self.assertAlmostEqual(cosine_similarity(v1, v2), 1.0, places=5)
        self.assertAlmostEqual(cosine_similarity(v1, v3), 0.0, places=5)

        # Ingestar una fuente de prueba
        txt = self.s.transcripts_dir / "Arquitectura_Clase01_Para_NotebookLM.txt"
        atomic_write(
            txt, "El examen final será en la semana 16 sobre arquitecturas limpias."
        )
        ingest_file(
            self.s,
            txt,
            course_id="arq-101",
            course_name="Arquitectura de Software",
            source_type="transcript",
        )
        self.s.gemini_api_key = "AIzaSyTestFakeKeyForUnitTest123456"
        self.s.use_semantic_embeddings = False

        called_models = []
        captured_configs = []

        def fake_generate_content(*, model, contents, config):
            called_models.append(model)
            captured_configs.append(config)
            if model == "gemini-3.6-flash":
                raise RuntimeError("429 RESOURCE_EXHAUSTED: Quota exceeded, retry in 15s")
            return NS(
                text="El examen final se realiza en la semana 16 `[Curso: Arquitectura de Software | Fuente: Arquitectura_Clase01]`."
            )

        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = fake_generate_content

        with patch("google.genai.Client", return_value=fake_client):
            engine = RAGEngine(self.s)
            resp = engine.ask(
                "¿Cuándo es el examen final?",
                course_id="arq-101",
                all_courses=False,
                preferred_model="gemini-3.6-flash",
            )

        self.assertEqual(called_models[0], "gemini-3.6-flash")
        self.assertEqual(resp.model_used, "gemini-3.5-flash-lite")
        self.assertEqual(resp.fallback_from, "gemini-3.6-flash")
        self.assertIn("semana 16", resp.answer)
        self.assertEqual(captured_configs[0].system_instruction, STRICT_SYSTEM_PROMPT)
        self.assertAlmostEqual(captured_configs[0].temperature, 0.1)

    def test_all_models_429_raises_rate_limit_error_with_retry_seconds(self):
        txt = self.s.transcripts_dir / "Curso_Clase01_Para_NotebookLM.txt"
        atomic_write(txt, "Contenido académico de prueba para verificar manejo 429.")
        ingest_file(self.s, txt, course_id="c1", course_name="Curso 1")
        self.s.gemini_api_key = "AIzaSyTestFakeKeyForUnitTest123456"
        self.s.use_semantic_embeddings = False

        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = RuntimeError(
            "429 RESOURCE_EXHAUSTED: retry in 22 seconds"
        )
        with patch("google.genai.Client", return_value=fake_client):
            engine = RAGEngine(self.s)
            with self.assertRaises(RateLimitExceededError) as ctx:
                engine.ask("¿Qué se vio en clase?", course_id="c1")
            self.assertEqual(ctx.exception.retry_after, 22)

    def test_model_fallback_on_503_high_demand(self):
        """Verifica que si un modelo (e.g. gemini-3.8-flash) devuelve 503 UNAVAILABLE por alta demanda,
        la consulta rote automáticamente al siguiente modelo disponible y agregue una nota explicativa."""
        txt = self.s.transcripts_dir / "Ciberseguridad_Semana01_Para_NotebookLM.txt"
        atomic_write(txt, "El modelo Zero Trust no confía en ninguna conexión por defecto.")
        ingest_file(self.s, txt, course_id="cyb-755", course_name="Ciberseguridad")
        self.s.gemini_api_key = "AIzaSyTestFakeKeyForUnitTest123456"
        self.s.use_semantic_embeddings = False

        called_models = []

        def fake_generate(*, model, contents, config):
            called_models.append(model)
            if model == "gemini-3.8-flash":
                raise RuntimeError(
                    "Error al consultar Gemini API: 503 UNAVAILABLE. {'error': {'code': 503, "
                    "'message': 'This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.', 'status': 'UNAVAILABLE'}}"
                )
            return NS(text="Zero Trust requiere autenticación continua y mínima confianza `[Curso: Ciberseguridad | Fuente: Ciberseguridad_Semana01]`.")

        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = fake_generate

        with patch("google.genai.Client", return_value=fake_client):
            engine = RAGEngine(self.s)
            resp = engine.ask(
                "¿Qué es Zero Trust?",
                course_id="cyb-755",
                preferred_model="gemini-3.8-flash",
            )

        self.assertEqual(called_models[0], "gemini-3.8-flash")
        # La cadena de conversación continúa con el modelo ligero de mayor cuota.
        self.assertEqual(resp.model_used, "gemini-3.5-flash-lite")
        self.assertEqual(resp.fallback_from, "gemini-3.8-flash")
        self.assertIn("Zero Trust", resp.answer)

    def test_all_models_503_raises_friendly_unavailable_error(self):
        """Verifica que si todos los modelos arrojan 503, se devuelva un mensaje amigable al estudiante."""
        txt = self.s.transcripts_dir / "Ciberseguridad_Semana01_Para_NotebookLM.txt"
        atomic_write(txt, "El modelo Zero Trust no confía en ninguna conexión por defecto.")
        ingest_file(self.s, txt, course_id="cyb-755", course_name="Ciberseguridad")
        self.s.gemini_api_key = "AIzaSyTestFakeKeyForUnitTest123456"
        self.s.use_semantic_embeddings = False

        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = RuntimeError(
            "503 UNAVAILABLE. This model is currently experiencing high demand."
        )

        with patch("google.genai.Client", return_value=fake_client):
            engine = RAGEngine(self.s)
            with self.assertRaises(RuntimeError) as ctx:
                engine.ask("¿Qué es Zero Trust?", course_id="cyb-755", preferred_model="gemini-3.8-flash")
            self.assertIn("503 UNAVAILABLE", str(ctx.exception))
            self.assertIn("alta demanda temporal", str(ctx.exception))

    def test_empty_scope_returns_strict_no_info_without_api_call(self):
        engine = RAGEngine(self.s)
        resp = engine.ask("¿Hay fecha de examen?", course_id="curso-inexistente")
        self.assertEqual(resp.answer, NO_INFO_RESPONSE)
        self.assertEqual(resp.model_used, "local-fts5")

    def test_api_key_encryption_and_redaction(self):
        secret_key = "AIzaSySecretKeyValueForTesting987654321"
        save_gemini_api_key(secret_key)
        self.assertEqual(load_gemini_api_key(), secret_key)
        key_file = self.root / "gemini_key.json"
        self.assertNotIn(secret_key, key_file.read_text(encoding="utf-8"))
        self.assertNotIn(secret_key, redact(f"Error con key={secret_key} o {secret_key}"))

    def test_assistant_view_ui_scope_toggle_and_429_preserves_question(self):
        """Verifica la pestaña Asistente IA en PySide6: toggle Todos los Cursos, fuentes y error 429."""
        t1 = self.s.transcripts_dir / "Calculo_Semana01_Para_NotebookLM.txt"
        t2 = self.s.transcripts_dir / "Fisica_Semana01_Para_NotebookLM.txt"
        atomic_write(t1, "Derivadas e integrales definidas.")
        atomic_write(t2, "Leyes de Newton y cinemática.")
        ingest_file(self.s, t1, course_id="calc", course_name="Cálculo I")
        ingest_file(self.s, t2, course_id="fis", course_name="Física I")

        with patch.object(HealthView, "refresh"):
            window = MainWindow(self.s)
        try:
            # Navegar a la nueva pestaña Asistente IA (índice 6)
            window._navigate(6)
            self.assertIs(window.stack.currentWidget(), window.assistant)
            self.assertFalse(window.body_widget.isVisible())

            view: AssistantView = window.assistant
            view._reload_courses()
            view._reload_sources_tree()

            # En modo de un solo curso, solo se listan las fuentes de ese curso
            self.assertFalse(view.is_all_courses_mode())
            self.assertEqual(len(view.get_active_source_ids()), 1)

            # Al activar "🌐 Consultar Todos los Cursos (Fuentes Compartidas)", incluye ambos cursos
            view.all_courses_btn.setChecked(True)
            self.assertTrue(view.is_all_courses_mode())
            self.assertFalse(view.course_combo.isEnabled())
            self.assertEqual(len(view.get_active_source_ids()), 2)

            # Verificar guía de configuración paso a paso dentro de la app
            from zoom2txt.gui.assistant_view import ApiKeyGuideDialog

            self.assertIn("aistudio.google.com/apikey", view.chat_view.toHtml())
            self.assertEqual(view.guide_btn.text(), "Cómo configurar")
            guide_dlg = ApiKeyGuideDialog(view)
            self.assertIn("1 minuto", guide_dlg.windowTitle())
            guide_dlg.close()

            # Simular pregunta y aviso 429: la pregunta escrita no debe perderse
            view.input_edit.setPlainText("¿Cuál es la relación entre derivada y velocidad?")
            view._on_rate_limited(15, "Límite alcanzado")
            self.assertEqual(
                view.input_edit.toPlainText(),
                "¿Cuál es la relación entre derivada y velocidad?",
            )
            self.assertFalse(view.rate_limit_banner.isHidden())
            self.assertIn("15 s", view.rate_limit_banner.text())
            view._countdown_timer.stop()

            # Verificar botones de estudio rápido y filtro de semanas
            self.assertEqual(len(view.quick_prompt_btns), 6)
            idx_parcial = view.week_filter_combo.findData("parcial")
            self.assertGreaterEqual(idx_parcial, 0)
            view.week_filter_combo.setCurrentIndex(idx_parcial)
            self.assertEqual(len(view.get_active_source_ids()), 2)
        finally:
            if hasattr(window, "assistant") and window.assistant:
                window.assistant.cancel_all()
            window.close()
            window.deleteLater()
            base.APP.processEvents()

    def test_week_filtering_and_quick_health_ai_assistant(self):
        """Verifica el filtrado por semanas (Parcial vs Final) en RAGEngine y el chequeo ai_assistant en quick_health."""
        from zoom2txt.health import pipeline_blockers, quick_health

        w2 = self.s.transcripts_dir / "Redes_Semana02_Para_NotebookLM.txt"
        w12 = self.s.transcripts_dir / "Redes_Semana12_Para_NotebookLM.txt"
        atomic_write(w2, "En la semana 2 revisamos el modelo OSI y direccionamiento IPv4.")
        atomic_write(w12, "En la semana 12 revisamos BGP, OSPF y enrutamiento dinámico.")
        ingest_file(self.s, w2, course_id="redes", course_name="Redes", source_type="transcript")
        ingest_file(self.s, w12, course_id="redes", course_name="Redes", source_type="transcript")

        engine = RAGEngine(self.s)
        parcial_chunks, _ = engine.retrieve(
            "modelo OSI BGP OSPF",
            course_id="redes",
            weeks=list(range(1, 8)),
            use_embeddings=False,
        )
        self.assertEqual(len(parcial_chunks), 1)
        self.assertIn("Semana02", parcial_chunks[0].source_title)

        final_chunks, _ = engine.retrieve(
            "modelo OSI BGP OSPF",
            course_id="redes",
            weeks=list(range(8, 17)),
            use_embeddings=False,
        )
        self.assertEqual(len(final_chunks), 1)
        self.assertIn("Semana12", final_chunks[0].source_title)

        # quick_health debe reportar ai_assistant como warning sin bloquear el pipeline cuando no hay API key
        self.s.gemini_api_key = ""
        save_gemini_api_key("")
        checks = {c.key: c for c in quick_health(self.s)}
        self.assertIn("ai_assistant", checks)
        self.assertEqual(checks["ai_assistant"].status, "warning")
        blocker_keys = {b.key for b in pipeline_blockers(self.s)}
        self.assertNotIn("ai_assistant", blocker_keys)

        # Con API key configurada pasa a 'ok'
        self.s.gemini_api_key = "AIzaSyFakeKeyForHealthCheckTest123"
        checks_ok = {c.key: c for c in quick_health(self.s)}
        self.assertEqual(checks_ok["ai_assistant"].status, "ok")


if __name__ == "__main__":
    unittest.main()

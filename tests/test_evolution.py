"""Zoom2TXT 3: progreso real, enrutado de modelos, contexto y citas del asistente,
herramientas de estudio y la interfaz nueva. Datos sintéticos; sin red."""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import test_hardening as base
from PySide6.QtCore import Qt

from zoom2txt import config, logs, study
from zoom2txt.ai_gateway import (
    AIAuthError,
    AIError,
    GeminiGateway,
    RateLimitExceededError,
    parse_json_payload,
)
from zoom2txt.ai_models import (
    AUTO_MODEL,
    ModelRouter,
    classify_error,
    describe,
    is_valid_model_id,
    model_label,
)
from zoom2txt.blackboard.registry import Registry, recording_key
from zoom2txt.download.zoom_downloader import parse_progress
from zoom2txt.gui.health_view import HealthView
from zoom2txt.gui.main_window import PAGE_ASSISTANT, PAGE_CLASSES, PAGE_HOME, MainWindow
from zoom2txt.processes import run_process
from zoom2txt.progress import ProgressLog, human_bytes, human_eta, report
from zoom2txt.rag_engine import (
    RAGEngine,
    cited_numbers,
    ingest_file,
    search_query,
    stitch_chunks,
)
from zoom2txt.storage import SCHEMA_VERSION, StateStore, atomic_write, digest_cached, file_ok
from zoom2txt.workflow import WorkLedger

KEY = "AIzaSyTestFakeKeyForUnitTest123456"


def long_text(seed: str, paragraphs: int = 14, words: int = 180) -> str:
    return "\n\n".join(
        " ".join(f"{seed}{p}_{w}" for w in range(words)) + f" tema {seed} número {p}"
        for p in range(paragraphs)
    )


class Case(unittest.TestCase):
    setUp = base.HardeningTests.setUp
    tearDown = base.HardeningTests.tearDown

    def source(self, name: str, text: str, course: str = "c1", course_name: str = "Curso 1") -> int:
        path = self.s.transcripts_dir / f"{name}_Para_NotebookLM.txt"
        atomic_write(path, text)
        return ingest_file(self.s, path, course_id=course, course_name=course_name)


class StorageAndPerformanceTests(Case):
    def test_schema_upgrade_backs_up_and_keeps_data(self):
        path = self.root / "old.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE records (namespace TEXT, kind TEXT, id TEXT, payload TEXT NOT NULL, "
                "updated REAL NOT NULL, PRIMARY KEY(namespace,kind,id))"
            )
            db.execute("INSERT INTO records VALUES ('ns', 'job', 'j1', '{\"status\":\"done\"}', 1.0)")
            db.execute("PRAGMA user_version=2")
        store = StateStore(self.s, path=path)
        backup = path.with_name(f"state-backup-v2-antes-de-v{SCHEMA_VERSION}.sqlite3")
        self.assertTrue(backup.is_file())
        with store.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records").fetchone()[0], 1)
            columns = {row[1] for row in db.execute("PRAGMA table_info(knowledge_sources)")}
            self.assertIn("word_count", columns)
            self.assertIn("meta_json", {r[1] for r in db.execute("PRAGMA table_info(chat_messages)")})
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        # Abrirla de nuevo no repite la migración ni crea otra copia.
        before = sorted(p.name for p in path.parent.glob("state-backup-*"))
        StateStore(self.s, path=path)
        self.assertEqual(before, sorted(p.name for p in path.parent.glob("state-backup-*")))

    def test_cached_digest_detects_changes(self):
        target = self.root / "clase.txt"
        atomic_write(target, "uno")
        first = digest_cached(target)
        self.assertEqual(first, digest_cached(target))
        self.assertTrue(file_ok(target, first, cached=True))
        atomic_write(target, "otro contenido")
        self.assertNotEqual(first, digest_cached(target))
        self.assertFalse(file_ok(target, first, cached=True))

    def test_processed_keys_matches_per_item_check(self):
        registry = Registry(self.s)
        done = self.root / "done.txt"
        atomic_write(done, base.VTT)
        registry.mark_done("c", "hecha", "Hecha", done)
        registry.mark_skipped("c", "omitida", "Omitida por el usuario")
        broken = self.root / "rota.txt"
        atomic_write(broken, base.VTT)
        registry.mark_done("c", "rota", "Rota", broken)
        atomic_write(broken, "modificada después")
        keys = registry.processed_keys()
        for meeting in ("hecha", "omitida", "rota", "inexistente"):
            self.assertEqual(
                recording_key("c", meeting) in keys, registry.is_processed("c", meeting), meeting
            )
        self.assertNotIn(recording_key("c", "rota"), keys)

    def test_overview_summarizes_inventory_by_week(self):
        ledger = WorkLedger(self.s)
        ledger.remember_courses([{"id": "c", "name": "Curso"}])
        ledger.remember_course(
            "c",
            "Curso",
            [
                dict(meeting_id="a", start_time="2026-03-31 18:00:00", duration=90, name="A"),
                dict(meeting_id="b", start_time="2026-04-07 18:00:00", duration=90, name="B"),
            ],
        )
        done = self.root / "a.txt"
        atomic_write(done, base.VTT)
        Registry(self.s).mark_done("c", "a", "A", done)
        overview = ledger.overview()
        self.assertEqual(overview["inventory"], 2)
        self.assertEqual(overview["transcribed"], 1)
        self.assertEqual([row["meeting_id"] for row in overview["pending"]], ["b"])
        self.assertEqual(overview["by_week"], {1: (1, 0), 2: (1, 1)})
        self.assertEqual(len(overview["pending"]), len(ledger.pending()))

    def test_chat_sessions_are_independent(self):
        store = StateStore(self.s)
        first = store.get_or_create_chat_session("c1", title="Curso")
        store.append_chat_message(first, "user", "hola", meta={"mode": "full"})
        second = store.create_chat_session("c1", title="Curso")
        self.assertNotEqual(first, second)
        self.assertEqual(store.get_or_create_chat_session("c1"), second)
        sessions = store.list_chat_sessions("c1")
        self.assertEqual([s["id"] for s in sessions], [second, first])
        self.assertEqual(sessions[1]["first_question"], "hola")
        self.assertEqual(store.get_chat_messages(first)[0]["meta"], {"mode": "full"})
        store.delete_chat_session(second)
        self.assertEqual(store.get_or_create_chat_session("c1"), first)

    def test_activity_log_is_redacted_and_rotates(self):
        with patch.object(config, "DEBUG_DIR", self.root / "debug"):
            logs.write("[i] acceso con password=secreto123 de alguien@example.com")
            content = logs.log_path().read_text(encoding="utf-8")
            self.assertNotIn("secreto123", content)
            self.assertNotIn("alguien@example.com", content)
            self.assertIn("[OCULTO]", content)
            with patch.object(logs, "MAX_BYTES", 200):
                for index in range(30):
                    logs.write(f"línea de relleno número {index} " + "x" * 40)
            self.assertTrue(logs.log_path().with_name("actividad.log.1").is_file())
            self.assertIn("relleno", logs.tail(5))


class ProgressTests(Case):
    def test_progress_channel_is_optional(self):
        events, lines = [], []
        log = ProgressLog(lines.append, lambda *args: events.append(args))
        log("hola")
        report(log, 1, 0.5, "mitad")
        report(log, 2, None, "sin porcentaje")
        report(print, 0, 0.1, "ignorado")
        report(lines.append, 0, 0.1, "ignorado")
        self.assertEqual(lines, ["hola"])
        self.assertEqual(events, [(1, 0.5, "mitad"), (2, -1.0, "sin porcentaje")])

    def test_download_progress_lines_are_parsed(self):
        fraction, detail = parse_progress("[z2t]|52428800|104857600|NA|2097152|25")
        self.assertAlmostEqual(fraction, 0.5)
        self.assertIn("50.0 MB de 100.0 MB", detail)
        self.assertIn("2.0 MB/s", detail)
        self.assertIn("faltan 25 s", detail)
        fraction, detail = parse_progress("[z2t]|1024|NA|NA|NA|NA")
        self.assertIsNone(fraction)
        self.assertEqual(detail, "1 KB")
        self.assertIsNone(parse_progress("[download] 10% of 5MiB"))
        self.assertIsNone(parse_progress("[z2t]|NA|NA"))
        self.assertEqual(human_bytes(1536 * 1024), "1.5 MB")
        self.assertEqual(human_eta(125), "2 min 05 s")
        self.assertEqual(human_eta(None), "")

    def test_streaming_process_delivers_lines_and_cancels(self):
        seen = []
        result = run_process(
            [sys.executable, "-c", "print('uno'); print('dos'); import sys; sys.stderr.write('aviso')"],
            on_line=seen.append,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(seen, ["uno", "dos"])
        self.assertIn("aviso", result.stderr)
        cancel = threading.Event()
        threading.Timer(0.4, cancel.set).start()
        started = time.monotonic()
        with self.assertRaises(InterruptedError):
            run_process(
                [sys.executable, "-c", "import time; print('x', flush=True); time.sleep(30)"],
                cancel=cancel,
                on_line=seen.append,
                timeout=60,
            )
        self.assertLess(time.monotonic() - started, 15)

    def test_upload_reports_real_progress(self):
        from zoom2txt.transcription import youtube_engine as yt

        events = []
        log = ProgressLog(lambda _t: None, lambda *args: events.append(args))
        steps = iter(
            [
                (NS(progress=lambda: 0.25, resumable_progress=25, total_size=100), None),
                (NS(progress=lambda: 0.75, resumable_progress=75, total_size=100), None),
                (None, {"id": "VIDEO123456"}),
            ]
        )
        request = NS(
            http=MagicMock(), resumable_uri=None, uri="u", next_chunk=lambda **_k: next(steps)
        )
        video = yt._complete_resumable_upload(request, log, None, None)
        self.assertEqual(video, "VIDEO123456")
        fractions = [event[1] for event in events if event[0] == 1]
        self.assertEqual(fractions, [-1.0, 0.25, 0.75])
        self.assertIn("de 100 B", events[-1][2])


class ModelRoutingTests(Case):
    def router(self, now=1_800_000_000.0):
        self.clock = [now]
        return ModelRouter(StateStore(self.s), clock=lambda: self.clock[0])

    def test_error_classification(self):
        cases = {
            "429 RESOURCE_EXHAUSTED. Please retry in 21.2s": ("rate_minute", 22),
            "429 quota GenerateRequestsPerDayPerProjectPerModel-FreeTier": ("rate_day", 30),
            "404 NOT_FOUND models/gemini-9 is not found": ("missing", 30),
            "400 API key not valid. Please pass a valid API key.": ("auth", 30),
            "503 UNAVAILABLE The model is overloaded": ("overloaded", 30),
            "400 INVALID_ARGUMENT response_json_schema unsupported": ("bad_request", 30),
            "getaddrinfo failed": ("network", 30),
        }
        for message, (kind, retry) in cases.items():
            info = classify_error(RuntimeError(message))
            self.assertEqual(info.kind, kind, message)
            if kind == "rate_minute":
                self.assertEqual(info.retry_after, retry)

    def test_auto_routes_by_task_and_skips_exhausted_models(self):
        router = self.router()
        chat = router.chain(AUTO_MODEL, "chat")
        deep = router.chain(AUTO_MODEL, "deep")
        self.assertEqual(describe(chat[0]).tier, "lite")
        self.assertEqual(describe(deep[0]).tier, "flash")
        self.assertEqual(set(chat), set(deep))
        router.note_error(deep[0], classify_error(RuntimeError("429 quota PerDay exhausted")))
        self.assertNotIn(deep[0], router.chain(AUTO_MODEL, "deep")[:-1])
        self.assertIn(deep[0], router.exhausted_today())
        # Otra instancia (otro arranque) recuerda el agotamiento hasta medianoche del Pacífico.
        again = ModelRouter(StateStore(self.s), clock=lambda: self.clock[0])
        self.assertFalse(again.is_usable(deep[0]))
        self.clock[0] += 26 * 3600
        self.assertTrue(again.is_usable(deep[0]))

    def test_minute_cooldown_and_explicit_choice(self):
        router = self.router()
        model = "gemini-3.8-flash"
        router.note_error(model, classify_error(RuntimeError("429 RESOURCE_EXHAUSTED retry in 30s")))
        self.assertFalse(router.is_usable(model))
        self.assertEqual(router.chain(model, "chat")[0], model)  # elegido a mano: se intenta
        self.assertNotEqual(router.chain(AUTO_MODEL, "deep")[0], model)
        self.clock[0] += 31
        self.assertTrue(router.is_usable(model))

    def test_discovery_filters_non_text_models_and_tracks_usage(self):
        router = self.router()
        listing = [
            NS(name="models/gemini-3.8-flash", supported_actions=["generateContent"]),
            NS(name="models/gemini-5.1-flash", supported_actions=["generateContent"]),
            NS(name="models/gemini-3.8-flash-tts", supported_actions=["generateContent"]),
            NS(name="models/gemini-embedding-001", supported_actions=["embedContent"]),
            NS(name="models/gemini-3.5-flash-lite", supported_actions=["generateContent"]),
            NS(name="models/gemini-3.5-flash-lite-001", supported_actions=["generateContent"]),
            NS(name="models/imagen-4", supported_actions=["predict"]),
        ]
        client = NS(models=NS(list=lambda: listing))
        found = router.refresh(client)
        self.assertEqual(
            set(found), {"gemini-3.8-flash", "gemini-5.1-flash", "gemini-3.5-flash-lite"}
        )
        ids = [info.id for info in router.available()]
        self.assertEqual(ids[:2], ["gemini-3.5-flash-lite", "gemini-3.8-flash"])
        self.assertIn("gemini-5.1-flash", ids)  # un modelo nuevo aparece sin actualizar la app
        self.assertNotIn("gemini-3.6-flash", ids)
        self.assertTrue(router.discovery_is_fresh())
        router.note_success("gemini-3.8-flash", 100, 20)
        router.note_success("gemini-3.8-flash", 50, 10)
        usage = router.usage_today()["gemini-3.8-flash"]
        self.assertEqual((usage["requests"], usage["input_tokens"]), (2, 150))
        self.assertIn("3.8 Flash 2", router.usage_summary())

    def test_model_ids_and_retired_preferences(self):
        self.assertTrue(is_valid_model_id("auto"))
        self.assertTrue(is_valid_model_id("gemini-4-hydrogen"))
        self.assertFalse(is_valid_model_id("gpt-9"))
        self.assertFalse(is_valid_model_id("gemini-3;drop"))
        self.assertEqual(model_label("auto"), "Automático")
        self.assertEqual(describe("gemini-9.9-flash-lite").tier, "lite")
        self.s.blackboard_url = "https://campus.example.edu"
        self.s.gemini_model = "gemini-3.5-flash"  # retirado por Google en 2026
        self.s.ai_grounding = "tutor"
        self.s.save()
        loaded = config.Settings.load()
        self.assertEqual(loaded.gemini_model, AUTO_MODEL)
        self.assertEqual(loaded.ai_grounding, "tutor")


class GatewayTests(Case):
    def gateway(self):
        return GeminiGateway(lambda: KEY, ModelRouter(StateStore(self.s)))

    def test_json_payload_extraction(self):
        self.assertEqual(parse_json_payload('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_json_payload('```json\n{"a": [1, 2]}\n```'), {"a": [1, 2]})
        self.assertEqual(parse_json_payload('Claro:\n[{"x": 1}]\nListo.'), [{"x": 1}])
        with self.assertRaises(ValueError):
            parse_json_payload("sin datos")

    def test_stream_restarts_cleanly_when_model_fails_midway(self):
        calls, pieces, resets = [], [], []

        def stream(*, model, contents, config):
            calls.append(model)
            if len(calls) == 1:
                yield NS(text="parcial ")
                raise RuntimeError("503 UNAVAILABLE overloaded")
            yield NS(text="respuesta ")
            yield NS(
                text="completa",
                usage_metadata=NS(prompt_token_count=40, candidates_token_count=6),
            )

        client = MagicMock()
        client.models.generate_content_stream.side_effect = stream
        with patch("google.genai.Client", return_value=client):
            gateway = self.gateway()
            result = gateway.generate(
                system="s",
                contents="c",
                preferred="gemini-3.8-flash",
                on_chunk=pieces.append,
                on_reset=lambda: resets.append(1),
            )
        self.assertEqual(result.text, "respuesta completa")
        self.assertEqual(calls[0], "gemini-3.8-flash")
        self.assertEqual(result.fallback_from, "gemini-3.8-flash")
        self.assertEqual((result.input_tokens, result.output_tokens), (40, 6))
        self.assertEqual(resets, [1])
        self.assertEqual(pieces, ["parcial ", "respuesta ", "completa"])
        self.assertEqual(gateway.router.usage_today()[result.model]["requests"], 1)

    def test_schema_rejection_retries_without_schema_on_same_model(self):
        configs = []

        def generate(*, model, contents, config):
            configs.append((model, config.response_json_schema))
            if config.response_json_schema is not None:
                raise RuntimeError("400 INVALID_ARGUMENT: response_json_schema is not supported")
            return NS(text='{"cards": []}')

        client = MagicMock()
        client.models.generate_content.side_effect = generate
        with patch("google.genai.Client", return_value=client):
            payload, result = self.gateway().generate_json(
                system="s", contents="c", json_schema={"type": "object"}
            )
        self.assertEqual(payload, {"cards": []})
        self.assertEqual(len(configs), 2)
        self.assertEqual(configs[0][0], configs[1][0])
        self.assertIsNone(configs[1][1])

    def test_invalid_key_stops_immediately_with_clear_message(self):
        client = MagicMock()
        client.models.generate_content.side_effect = RuntimeError(
            "400 INVALID_ARGUMENT API key not valid. Please pass a valid API key."
        )
        with patch("google.genai.Client", return_value=client):
            with self.assertRaises(AIAuthError) as ctx:
                self.gateway().generate(system="s", contents="c")
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.assertIn("aistudio.google.com/apikey", str(ctx.exception))
        self.assertNotIn(KEY, str(ctx.exception))

    def test_daily_quota_everywhere_is_reported_as_daily(self):
        client = MagicMock()
        client.models.generate_content.side_effect = RuntimeError(
            "429 RESOURCE_EXHAUSTED quota GenerateRequestsPerDayPerProjectPerModel-FreeTier"
        )
        with patch("google.genai.Client", return_value=client):
            gateway = self.gateway()
            with self.assertRaises(RateLimitExceededError) as ctx:
                gateway.generate(system="s", contents="c")
        self.assertTrue(ctx.exception.daily)
        self.assertGreaterEqual(len(gateway.router.exhausted_today()), 3)

    def test_missing_key_is_an_auth_error(self):
        gateway = GeminiGateway(lambda: "", ModelRouter(StateStore(self.s)))
        self.assertFalse(gateway.has_key())
        with self.assertRaises(AIAuthError):
            gateway.generate(system="s", contents="c")


class KnowledgeEngineTests(Case):
    def test_text_helpers(self):
        words = [f"w{i}" for i in range(300)]
        first, second = " ".join(words[:200]), " ".join(words[100:300])
        self.assertEqual(stitch_chunks([first, second]).split(), words)
        self.assertEqual(stitch_chunks(["uno dos", "tres cuatro"]), "uno dos\n\ntres cuatro")
        self.assertEqual(cited_numbers("a [1] b [2, 3] c [5-7] d [1] e [9 y 10]"), [1, 2, 3, 5, 6, 7, 9, 10])
        self.assertEqual(cited_numbers("vector [x] y lista [a, b]"), [])
        history = [{"role": "user", "content": "Explica la varianza"}, {"role": "assistant", "content": "…"}]
        self.assertIn("varianza", search_query("¿y eso cómo se calcula?", history))
        standalone = "¿Qué diferencia hay entre covarianza y correlación de Pearson?"
        self.assertEqual(search_query(standalone, history), standalone)
        self.assertEqual(search_query("hola", None), "hola")

    def test_small_scope_uses_whole_sources_and_large_scope_retrieves(self):
        sid = self.source("Redes_Semana02", long_text("osi"))
        engine = RAGEngine(self.s)
        self.assertGreater(engine.store.list_knowledge_sources(all_courses=True)[0]["word_count"], 2000)
        plan = engine.build_context("modelo", course_id="c1")
        self.assertEqual(plan.mode, "full")
        self.assertEqual(len(plan.passages), 1)
        self.assertTrue(plan.passages[0].whole)
        self.assertEqual(plan.passages[0].source_id, sid)
        self.assertEqual(len(plan.passages[0].text.split()), plan.words)
        with patch.dict("zoom2txt.rag_engine.FULL_CONTEXT_WORDS", {"chat": 500, "deep": 500}):
            retrieval = engine.build_context("tema osi número 3", course_id="c1", top_k=2)
            self.assertEqual(retrieval.mode, "retrieval")
            self.assertLess(retrieval.words, plan.words)
            sample = engine.build_context("guía", course_id="c1", task="deep")
            self.assertEqual(sample.mode, "sample")
            self.assertTrue(sample.passages)
        self.assertEqual(engine.build_context("x", course_id="otro").mode, "none")
        self.assertEqual(engine.build_context("x", course_id="c1", active_source_ids=[]).mode, "none")

    def test_adjacent_fragments_merge_into_one_numbered_passage(self):
        self.source("Algebra_Semana03", long_text("matriz"))
        engine = RAGEngine(self.s)
        chunks, _ = engine.retrieve("tema matriz", course_id="c1", top_k=8, use_embeddings=False)
        passages = engine.passages_from_chunks(chunks)
        self.assertEqual([p.number for p in passages], list(range(1, len(passages) + 1)))
        self.assertLess(len(passages), len(chunks))
        block = engine.format_sources_block(passages)
        self.assertTrue(block.startswith("<fuentes>") and block.endswith("</fuentes>"))
        self.assertIn("[1] Curso: Curso 1 · Clase: Algebra_Semana03", block)

    def test_ask_sends_history_sources_and_returns_numbered_citations(self):
        self.source("Stats_Semana01", "La varianza mide la dispersión respecto de la media.")
        self.source("Stats_Semana02", "La covarianza relaciona dos variables aleatorias.")
        self.s.gemini_api_key = KEY
        self.s.use_semantic_embeddings = False
        captured = {}

        def generate(*, model, contents, config):
            captured.update(model=model, contents=contents, config=config)
            return NS(
                text="La varianza mide la dispersión [1]. No aparece en [7].",
                usage_metadata=NS(prompt_token_count=120, candidates_token_count=15),
            )

        client = MagicMock()
        client.models.generate_content.side_effect = generate
        history = [
            {"role": "user", "content": "¿Qué vimos en la primera clase?"},
            {"role": "assistant", "content": "Vimos medidas de dispersión [1]."},
        ]
        with patch("google.genai.Client", return_value=client):
            engine = RAGEngine(self.s)
            resp = engine.ask("¿y eso cómo se interpreta?", course_id="c1", history=history)
        contents = captured["contents"]
        self.assertEqual([c["role"] for c in contents], ["user", "model", "user"])
        final = contents[-1]["parts"][0]["text"]
        self.assertIn("<fuentes>", final)
        self.assertIn("[1] Curso: Curso 1 · Clase: Stats_Semana01", final)
        self.assertIn("[2] Curso: Curso 1 · Clase: Stats_Semana02", final)
        self.assertIn("¿y eso cómo se interpreta?", final)
        self.assertEqual(resp.mode, "full")
        self.assertEqual([c["n"] for c in resp.citations], [1])  # el 7 no existe: no se cita
        self.assertEqual(resp.citations[0]["source_title"], "Stats_Semana01")
        self.assertTrue(resp.citations[0]["path"].endswith("Stats_Semana01_Para_NotebookLM.txt"))
        self.assertEqual((resp.input_tokens, resp.output_tokens), (120, 15))
        self.assertEqual(describe(captured["model"]).tier, "lite")

    def test_tutor_mode_and_general_answers_are_labelled(self):
        from zoom2txt.rag_engine import GENERAL_NOTE, STRICT_SYSTEM_PROMPT, TUTOR_SYSTEM_PROMPT

        self.source("Fisica_Semana01", "La segunda ley de Newton relaciona fuerza y aceleración.")
        self.s.gemini_api_key = KEY
        self.s.use_semantic_embeddings = False
        systems = []

        def generate(*, model, contents, config):
            systems.append(config.system_instruction)
            return NS(text="Respuesta sin numerar.")

        client = MagicMock()
        client.models.generate_content.side_effect = generate
        with patch("google.genai.Client", return_value=client):
            engine = RAGEngine(self.s)
            strict = engine.ask("¿Qué dice Newton?", course_id="c1")
            tutor = engine.ask("¿Qué dice Newton?", course_id="c1", grounding="tutor")
            general = engine.ask("¿Qué es una integral?", course_id="c1", active_source_ids=[])
        self.assertEqual(systems[0], STRICT_SYSTEM_PROMPT)
        self.assertEqual(systems[1], TUTOR_SYSTEM_PROMPT)
        self.assertEqual(tutor.grounding, "tutor")
        # Sin números en la respuesta se conserva la procedencia como cita implícita.
        self.assertTrue(strict.citations and strict.citations[0]["implicit"])
        self.assertEqual(general.mode, "general")
        self.assertTrue(general.answer.startswith(GENERAL_NOTE))
        self.assertEqual(general.citations, [])

    def test_full_context_over_quota_falls_back_to_fragments(self):
        self.source("Quimica_Semana04", long_text("enlace"))
        self.s.gemini_api_key = KEY
        self.s.use_semantic_embeddings = False
        sizes = []

        def generate(*, model, contents, config):
            size = len(contents[-1]["parts"][0]["text"].split())
            sizes.append(size)
            if size > 1800:
                raise RuntimeError("429 RESOURCE_EXHAUSTED tokens per minute, retry in 12s")
            return NS(text="Con fragmentos [1].")

        client = MagicMock()
        client.models.generate_content.side_effect = generate
        resets = []
        with patch("google.genai.Client", return_value=client):
            engine = RAGEngine(self.s)
            resp = engine.ask(
                "tema enlace número 2", course_id="c1", top_k=1, on_reset=lambda: resets.append(1)
            )
        self.assertEqual(resp.mode, "retrieval")
        self.assertEqual(resp.answer, "Con fragmentos [1].")
        self.assertLess(sizes[-1], sizes[0])
        self.assertEqual(resets, [1])

    def test_semantic_rerank_uses_task_types_and_caches_vectors(self):
        import numpy as np

        self.source("Bio_Semana01", "La mitocondria produce energía celular mediante respiración.")
        self.source("Bio_Semana02", "El cloroplasto realiza la fotosíntesis en células vegetales.")
        self.s.gemini_api_key = KEY
        self.s.use_semantic_embeddings = True
        calls = []

        def embed(*, model, contents, config):
            calls.append((config.task_type, contents))
            if config.task_type == "RETRIEVAL_QUERY":
                return NS(embeddings=[NS(values=[0.0, 1.0, 0.0])])
            texts = contents if isinstance(contents, list) else [contents]
            return NS(
                embeddings=[
                    NS(values=[0.0, 1.0, 0.0] if "cloroplasto" in t else [1.0, 0.0, 0.0])
                    for t in texts
                ]
            )

        client = MagicMock()
        client.models.embed_content.side_effect = embed
        with patch("google.genai.Client", return_value=client):
            engine = RAGEngine(self.s)
            chunks, used = engine.retrieve("energía de la planta", course_id="c1")
            self.assertTrue(used)
            self.assertIn("cloroplasto", chunks[0].content)
            self.assertEqual([c[0] for c in calls], ["RETRIEVAL_QUERY", "RETRIEVAL_DOCUMENT"])
            engine.retrieve("energía de la planta", course_id="c1")
        self.assertEqual([c[0] for c in calls][2:], ["RETRIEVAL_QUERY"])  # documentos ya en caché
        stats = engine.store.knowledge_stats()
        self.assertEqual((stats["sources"], stats["embedded"]), (2, 2))
        self.assertEqual(np.dtype("float32").itemsize * 3, 12)

    def test_folder_sync_never_moves_a_source_to_a_guessed_course(self):
        from zoom2txt.rag_engine import sync_existing_transcripts

        sid = self.source("Calculo_Semana01", "Derivadas e integrales.", "calc", "Cálculo I")
        stats = sync_existing_transcripts(self.s)
        self.assertEqual(stats["indexed"], 1)
        source = StateStore(self.s).list_knowledge_sources(all_courses=True)[0]
        self.assertEqual((source["id"], source["course_id"], source["course_name"]), (sid, "calc", "Cálculo I"))
        # Un curso indicado de forma explícita sí reubica la fuente.
        path = Path(source["file_path"])
        ingest_file(self.s, path, course_id="mate", course_name="Matemática")
        moved = StateStore(self.s).list_knowledge_sources(all_courses=True)[0]
        self.assertEqual((moved["id"], moved["course_id"]), (sid, "mate"))
        # Y una fuente sin curso conocido adopta el que la app logre identificar.
        loose = self.s.transcripts_dir / "Suelto_Para_NotebookLM.txt"
        atomic_write(loose, "Texto suelto.")
        loose_id = ingest_file(self.s, loose, course_id="general", course_name="General")
        ingest_file(self.s, loose)
        again = {s["id"]: s for s in StateStore(self.s).list_knowledge_sources(all_courses=True)}
        self.assertEqual(again[loose_id]["course_id"], "Suelto")

    def test_local_full_text_search_needs_no_key(self):
        self.source("Historia_Semana05", "El virreinato del Perú se creó en 1542 por Carlos I.")
        engine = RAGEngine(self.s)
        hits = engine.search_text("virreinato")
        self.assertEqual(len(hits), 1)
        self.assertIn("[[virreinato]]", hits[0]["snippet"])
        self.assertEqual(hits[0]["title"], "Historia_Semana05")
        self.assertEqual(engine.search_text("inexistente"), [])
        self.assertEqual(engine.search_text("   "), [])
        self.assertIn("1542", engine.source_text(hits[0]["source_id"]))

    def test_study_tools_parse_validate_and_persist(self):
        self.source("Derecho_Semana02", long_text("contrato", paragraphs=4))
        self.s.gemini_api_key = KEY
        self.s.use_semantic_embeddings = False
        captured = {}

        def generate(*, model, contents, config):
            captured.update(model=model, mime=config.response_mime_type, contents=contents)
            if "simulacro" in contents:
                return NS(
                    text='{"questions": [{"question": "¿Qué es un contrato?", "options": ["A1", "B1", "C1", "D1"], '
                    '"answer": "B", "explanation": "Acuerdo de voluntades.", "source": 1}, '
                    '{"question": "mala", "options": ["sola"], "answer": 0}]}'
                )
            return NS(
                text='```json\n{"cards": [{"front": "Contrato", "back": "Acuerdo de voluntades", "source": 1}, '
                '{"front": "contrato", "back": "duplicada"}, {"front": "", "back": "vacía"}]}\n```'
            )

        client = MagicMock()
        client.models.generate_content.side_effect = generate
        with patch("google.genai.Client", return_value=client):
            engine = RAGEngine(self.s)
            cards = engine.generate_study("flashcards", course_id="c1", count=6, title="Derecho")
            quiz = engine.generate_study("quiz", course_id="c1", focus="obligaciones")
            with self.assertRaises(RuntimeError):
                engine.generate_study("flashcards", course_id="c1", active_source_ids=[])
            with self.assertRaises(ValueError):
                engine.generate_study("resumen", course_id="c1")
        self.assertEqual(captured["mime"], "application/json")
        self.assertEqual(describe(captured["model"]).tier, "flash")  # tarea profunda
        self.assertIn("obligaciones", captured["contents"])
        self.assertEqual(len(cards.items), 1)
        self.assertEqual(cards.citations[0]["n"], 1)
        self.assertEqual(len(quiz.items), 1)
        self.assertEqual(quiz.items[0]["answer"], 1)  # «B» → índice 1
        store = engine.store
        deck_id = study.save_deck(store, quiz, "c1")
        study.record_attempt(store, deck_id, 1, 1)
        saved = study.list_decks(store, "quiz")[0]
        self.assertEqual((saved["title"], saved["attempts"][0]["score"]), ("Simulacro", 1))
        study.delete_deck(store, deck_id)
        self.assertEqual(study.list_decks(store), [])
        self.assertEqual(study.flashcards_to_csv(cards.items), "Contrato,Acuerdo de voluntades\n")
        exported = study.quiz_to_markdown("Derecho", quiz.items, {0: 2})
        self.assertIn("- [x] B) B1", exported)
        self.assertIn("C) C1  ← tu respuesta", exported)

    def test_bad_study_payload_is_a_friendly_error(self):
        self.source("Etica_Semana01", "La ética estudia la moral.")
        self.s.gemini_api_key = KEY
        client = MagicMock()
        client.models.generate_content.return_value = NS(text='{"cards": []}')
        with patch("google.genai.Client", return_value=client):
            with self.assertRaises(AIError) as ctx:
                RAGEngine(self.s).generate_study("flashcards", course_id="c1")
        self.assertIn("tarjetas válidas", str(ctx.exception))


class InterfaceTests(Case):
    def setUp(self):
        base.HardeningTests.setUp(self)
        self._widgets = []

    def tearDown(self):
        # Los hilos de la ventana deben terminar antes de borrar la carpeta temporal.
        for widget in reversed(self._widgets):
            assistant = getattr(widget, "assistant", None)
            if assistant is not None:
                assistant.cancel_all()
            widget.close()
            widget.deleteLater()
        base.APP.processEvents()
        base.HardeningTests.tearDown(self)

    def window(self):
        with patch.object(HealthView, "refresh"):
            window = MainWindow(self.s)
        self._widgets.append(window)
        return window

    def keep(self, widget):
        self._widgets.append(widget)
        return widget

    def test_markdown_renderer_links_only_real_citations(self):
        from zoom2txt.gui.assistant_view import render_markdown_to_html

        html = render_markdown_to_html(
            "## Título\n\nTexto **fuerte** con cita [1] y falsa [9].\n\n- uno\n- dos\n\n"
            "| A | B |\n|---|---|\n| 1 | 2 |\n\n```python\nprint('<hola>')\n```\n\n> nota",
            {1, 2},
        )
        self.assertIn('href="cite:1"', html)
        self.assertNotIn('href="cite:9"', html)
        self.assertIn("[9]", html)
        self.assertIn("<b>fuerte</b>", html)
        self.assertIn("<li", html)
        self.assertIn("<th", html)
        self.assertIn("print(&#x27;&lt;hola&gt;&#x27;)", html)
        self.assertNotIn("<script", render_markdown_to_html("<script>alert(1)</script>"))
        self.assertEqual(render_markdown_to_html(""), "")
        legacy = render_markdown_to_html("Dato [Curso: Redes | Fuente: Clase 1].")
        self.assertIn("Curso: Redes | Fuente: Clase 1", legacy)

    def test_window_opens_on_home_and_keeps_page_indices(self):
        window = self.window()
        self.assertIs(window.stack.currentWidget(), window.home)
        self.assertEqual(len(window._nav_buttons), 8)
        self.assertTrue(window._nav_buttons[PAGE_HOME].isChecked())
        self.assertFalse(window.body_widget.isVisible())
        window.show()
        window._navigate(PAGE_CLASSES)
        self.assertTrue(window.body_widget.isVisible())
        self.assertEqual(window.page_title.text(), "Tus clases")
        window._navigate(PAGE_ASSISTANT)
        self.assertIs(window.stack.currentWidget(), window.assistant)
        self.assertEqual(len(window.stage_rows), 4)
        self.assertLess(window.minimumSizeHint().width(), 1180)

    def test_pipeline_progress_reaches_stepper_and_status_line(self):
        window = self.window()
        window._queue_total = 3
        window._active_queue_index = 1
        window._queue_items = []
        window._active_queue_index = None
        window._on_stage(0)
        window._on_progress(0, 0.5, "50 de 100 MB")
        self.assertEqual(window.stepper.state(0), "active")
        self.assertAlmostEqual(window.stepper._progress[0], 0.5)
        self.assertIn("Descarga 50%", window.dock_text.fullText())
        window._on_progress(1, 0.9, "no es el paso activo")
        self.assertNotIn(1, window.stepper._progress)
        window._on_stage(1)
        self.assertEqual(window.stepper.state(0), "done")
        self.assertNotIn(0, window.stepper._progress)
        window._on_round_wait(95, 2)
        self.assertIn("1:35", window.dock_text.fullText())
        self.assertEqual(window.dock_badge.text(), "Esperando texto")
        window._reset_stages()
        self.assertEqual([row.state for row in window.stage_rows], ["pending"] * 4)

    def test_header_click_toggles_every_selectable_row(self):
        window = self.window()
        window._on_scan_found(
            [
                dict(course_id="c", course_name="Curso", meeting_id=f"m{i}", name=f"Clase{i}",
                     start_time="2026-04-01 10:00:00", sort_key="2026-04-01", play_url="")
                for i in range(3)
            ]
        )  # fmt: skip
        locked = window.table.item(2, 0)
        locked.setFlags(locked.flags() & ~Qt.ItemFlag.ItemIsEnabled)

        def states():
            return [window.table.item(r, 0).checkState() == Qt.CheckState.Checked for r in range(3)]

        self.assertEqual(states(), [True, True, True])
        window._toggle_all_rows(0)
        self.assertEqual(states(), [False, False, True])  # la fila ya en cola no cambia
        window._toggle_all_rows(3)
        self.assertEqual(states(), [False, False, True])
        window._toggle_all_rows(0)
        self.assertEqual(states(), [True, True, True])

    def test_home_reflects_inventory_and_quota(self):
        ledger = WorkLedger(self.s)
        ledger.remember_courses([{"id": "c", "name": "Cálculo I"}])
        ledger.remember_course(
            "c",
            "Cálculo I",
            [dict(meeting_id="a", start_time="2026-03-31 18:00:00", duration=90, name="A")],
        )
        ledger.admit("otra-clase")
        window = self.window()
        window._refresh_backlog_notice()
        self.assertIn("1 clase por transcribir", window.home.headline.text())
        self.assertEqual(window.home.kpi_pending.value_label.text(), "1")
        self.assertEqual(window.home.kpi_today.value_label.text(), "1/6")
        self.assertEqual(window._nav_buttons[4].badge(), "1")
        self.assertEqual(window.quota_title.text(), "Hoy 1/6 clases")
        self.assertFalse(window.home.next_host.isHidden())

    def test_command_palette_ranks_actions_and_searches_content(self):
        from zoom2txt.gui.command_palette import CommandPalette, match_score

        self.assertGreater(match_score("bib", "Ir a Biblioteca"), 0)
        self.assertGreater(match_score("Ir a", "Ir a Biblioteca"), match_score("bib", "Ir a Biblioteca"))
        self.assertGreater(match_score("configuracion", "Abrir configuración"), 0)  # sin tilde
        self.assertEqual(match_score("zzz", "Ir a Biblioteca"), 0)
        self.source("Economia_Semana03", "La elasticidad precio de la demanda mide sensibilidad.")
        window = self.window()
        ran = []
        actions = window._palette_actions()
        self.assertTrue(any(a["title"] == "Buscar clases nuevas" for a in actions))
        palette = CommandPalette(
            window,
            [dict(icon="search", title="Ir a Biblioteca", hint="", run=lambda: ran.append("lib"))],
            window._palette_search,
        )
        entries = palette.entries_for("elasticidad")
        self.assertEqual(entries[-1]["title"], "Economia_Semana03")
        self.assertIn("[[elasticidad]]", entries[-1]["snippet"])
        self.assertEqual([e["title"] for e in palette.entries_for("bibl")], ["Ir a Biblioteca"])
        palette.close()

    def test_reader_finds_and_counts_matches(self):
        from zoom2txt.gui.reader_dialog import ReaderDialog

        path = self.s.transcripts_dir / "Lectura_Semana01_Para_NotebookLM.txt"
        atomic_write(path, "La derivada mide el cambio.\n\nOtra derivada parcial.\n\nFin.")
        reader = ReaderDialog(self.s, path, query="¿qué es la derivada?")
        self.keep(reader)
        self.assertEqual(reader.search.text(), "derivada")
        self.assertEqual(reader.match_count(), 2)
        self.assertEqual(reader.count_label.text(), "1/2")
        reader.find_next()
        self.assertEqual(reader.count_label.text(), "2/2")
        reader.find_next()
        self.assertEqual(reader.count_label.text(), "1/2")
        self.assertIn("palabras", reader.meta_label.text())
        self.assertEqual(reader.title_label.fullText(), "Lectura_Semana01")

    def test_library_filters_by_name_and_finds_content(self):
        from zoom2txt import library

        self.source("Redes_Semana02", "El protocolo BGP intercambia rutas entre sistemas autónomos.")
        self.source("Calculo_Semana01", "La integral definida calcula el área bajo la curva.")
        window = self.window()
        view = window.library
        view._loaded(library.scan_library(self.s))
        self.assertEqual(view.table.rowCount(), 2)
        self.assertEqual(view.card_txt.value_label.text(), "2")
        view.search.setText("calculo")
        view._apply_filter()
        visible = [r for r in range(2) if not view.table.isRowHidden(r)]
        self.assertEqual(len(visible), 1)
        self.assertEqual(view.table.item(visible[0], 0).text(), "Calculo_Semana01")
        view.search.setText("BGP")
        view._apply_filter()
        self.assertEqual(len(view._hits), 1)
        self.assertFalse(view.hits_list.isHidden())
        asked = []
        view.ask_requested.connect(asked.append)
        view.ask_requested.emit(view._hits[0]["path"])
        self.assertTrue(asked[0].endswith("Redes_Semana02_Para_NotebookLM.txt"))

    def test_assistant_focus_sessions_and_streamed_answer(self):
        from zoom2txt.rag_engine import RAGResponse

        first = self.source("Calculo_Semana01", "Derivadas e integrales definidas.", "calc", "Cálculo I")
        self.source("Calculo_Semana02", "Series de Taylor y convergencia.", "calc", "Cálculo I")
        window = self.window()
        window._navigate(PAGE_ASSISTANT)
        view = window.assistant
        view._reload_courses()
        view._reload_sources_tree()
        self.assertEqual(len(view.get_active_source_ids()), 2)
        window._ask_about_source(str(self.s.transcripts_dir / "Calculo_Semana01_Para_NotebookLM.txt"))
        self.assertEqual(view.get_active_source_ids(), [first])
        self.assertIn("caben completas", view.context_meter.text())

        # Respuesta en streaming: burbujas provisionales y persistencia al terminar.
        view._pending_question = "¿Qué es una derivada?"
        view._pending_user = view.chat_view.add_message("user")
        view._pending_user.set_user_text("¿Qué es una derivada?")
        view._pending_reply = view.chat_view.add_message("assistant")
        view._on_chunk("La derivada ")
        view._on_chunk("mide el cambio [1].")
        view._paint_stream()
        self.assertIn("mide el cambio", view.chat_view.toHtml())
        citation = {"n": 1, "source_id": first, "source_title": "Calculo_Semana01", "course_name": "Cálculo I",
                    "source_type": "transcript", "locator": "clase completa", "path": "", "snippet": "Derivadas"}
        view._on_query_done(
            "¿Qué es una derivada?",
            RAGResponse(
                answer="La derivada mide el cambio [1].", citations=[citation], chunks=[],
                model_used="gemini-3.5-flash-lite", mode="full", input_tokens=30, output_tokens=9,
            ),
        )  # fmt: skip
        messages = view.store.get_chat_messages(view._current_session_id)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        self.assertEqual(messages[1]["meta"]["mode"], "full")
        self.assertEqual(messages[1]["citations"][0]["n"], 1)
        self.assertIn('href="cite:1"', view.chat_view.toHtml())
        reply = view.chat_view.messages()[-1]
        self.assertEqual(reply.citation(1)["source_title"], "Calculo_Semana01")
        self.assertFalse(reply.sources_host.isHidden())

        # Nueva conversación en el mismo curso; la anterior sigue disponible.
        previous = view._current_session_id
        view._new_chat()
        self.assertNotEqual(view._current_session_id, previous)
        self.assertEqual(view.chat_view.messages(), [])
        view._select_session(previous)
        self.assertEqual(len(view.chat_view.messages()), 2)

        # Un fallo devuelve la pregunta al cuadro de texto y quita lo provisional.
        view._pending_question = "pregunta pendiente"
        view._pending_user = view.chat_view.add_message("user")
        view._pending_reply = view.chat_view.add_message("assistant")
        view._on_query_failed("sin conexión")
        self.assertEqual(view.input_edit.toPlainText(), "pregunta pendiente")
        self.assertEqual(len(view.chat_view.messages()), 2)
        self.assertEqual(view.rate_limit_banner.tone(), "error")
        view._on_rate_limited(3600, "cuota diaria")
        self.assertIn("cuota gratuita diaria", view.rate_limit_banner.text())
        self.assertFalse(view._countdown_timer.isActive())

    def test_flashcards_and_quiz_dialogs(self):
        from zoom2txt.gui.study_dialogs import FlashcardsDialog, QuizDialog

        cards = study.StudyResult(
            kind="flashcards", tool="flashcards", title="Mazo",
            items=[{"front": f"P{i}", "back": f"R{i}", "source": 0} for i in range(3)],
        )  # fmt: skip
        deck = FlashcardsDialog(cards)
        self.keep(deck)
        self.assertEqual(deck.text.text(), "P0")
        deck.flip()
        self.assertEqual(deck.text.text(), "R0")
        deck._mark(True)
        self.assertEqual((deck.text.text(), deck.counter.text()), ("P1", "2 / 3"))
        deck._mark(False)
        self.assertIn("1 sabidas · 1 por repasar", deck.summary.text())

        store = StateStore(self.s)
        quiz = study.StudyResult(
            kind="quiz", tool="quiz", title="Examen",
            items=[
                {"question": "2+2", "options": ["3", "4", "5", "6"], "answer": 1, "explanation": "Suma.", "source": 0},
                {"question": "3+3", "options": ["6", "7", "8", "9"], "answer": 0, "explanation": "", "source": 0},
            ],
        )  # fmt: skip
        study.save_deck(store, quiz, "c1")
        dialog = QuizDialog(quiz, store)
        self.keep(dialog)
        self.assertFalse(dialog.next_btn.isEnabled())
        dialog.choose(1)
        dialog.choose(2)  # ya respondida: se ignora
        self.assertEqual(dialog.feedback.tone(), "success")
        dialog._next()
        dialog.choose(3)
        self.assertEqual(dialog.feedback.tone(), "error")
        self.assertIn("La respuesta es A", dialog.feedback.text())
        dialog._next()
        self.assertEqual(dialog.score(), (1, 2))
        self.assertEqual(dialog.pages.currentIndex(), 1)
        self.assertEqual(study.list_decks(store, "quiz")[0]["attempts"][0]["score"], 1)
        dialog.restart()
        self.assertEqual(dialog.pages.currentIndex(), 0)

    def test_status_tones_and_theme_switch(self):
        from zoom2txt.gui import style, widgets

        self.assertEqual(widgets.status_tone("TXT verificado"), "success")
        self.assertEqual(widgets.status_tone("Por resolver"), "warning")
        self.assertEqual(widgets.status_tone("Texto en proceso"), "info")
        self.assertEqual(widgets.status_tone("Acción necesaria"), "error")
        self.assertEqual(widgets.status_tone("Por descargar"), "")
        self.s.blackboard_url = "https://campus.example.edu"
        window = self.window()
        original = style.current_mode
        try:
            window._toggle_theme()
            self.assertNotEqual(style.current_mode, original)
            self.assertEqual(config.Settings.load().theme, style.current_mode)
        finally:
            style.apply_theme(base.APP, original)
            self.s.theme = original


if __name__ == "__main__":
    unittest.main()

import { useEffect, useRef, useState } from "react";
import ResultChart from "./ResultChart.jsx";

const api = async (path, options = {}) => {
  const { headers: extra, ...rest } = options;
  const res = await fetch(path, {
    ...rest,
    headers: { "Content-Type": "application/json", ...(extra || {}) },
    credentials: "same-origin",
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    // `detail` is a plain string for expected refusals, and an object carrying
    // a stable `code` (and, for an unhandled failure, a request id) otherwise.
    // Showing the id gives the user something to quote when reporting it.
    const detail = body.detail;
    const message =
      typeof detail === "string"
        ? detail
        : detail?.message || "Something went wrong.";
    const err = new Error(
      detail?.request_id ? `${message} (reference ${detail.request_id})` : message,
    );
    err.requestId = detail?.request_id;
    err.status = res.status;
    err.code = typeof detail === "object" ? detail?.code : undefined;
    err.retryAfter = res.headers?.get?.("Retry-After");
    throw err;
  }
  return body;
};

// One key per question, reused on a retry of that question. The server keeps
// one committed outcome per key, so resending after a dropped connection
// returns the answer that was already computed instead of asking twice.
const newKey = () =>
  globalThis.crypto?.randomUUID?.() ??
  `k-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function Login({ onSignedIn, notice }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [sso, setSso] = useState(false);

  // Offered only when the server says it is configured; any failure to ask
  // simply leaves password sign-in, which always works.
  useEffect(() => {
    api("/api/auth/methods").then((m) => setSso(Boolean(m?.oidc))).catch(() => {});
  }, []);

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { user } = await api("/api/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      onSignedIn(user);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-shell">
      <form className="login" onSubmit={submit}>
        <h1>Pharma Analytics Copilot</h1>
        <p className="muted">
          Sign in to ask questions about commercial performance. What you can see
          depends on your role.
        </p>
        <label>
          Email
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="username"
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        {notice && !error && <div className="notice">{notice}</div>}
        {error && <div className="error">{error}</div>}
        <button type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        {sso && (
          <a className="sso" href="/api/auth/oidc/start">
            Sign in with single sign-on
          </a>
        )}
      </form>
    </div>
  );
}

export function ResultTable({ answer }) {
  if (!answer?.rows?.length) return null;
  const dimensionKeys = Object.keys(answer.rows[0]).filter(
    (k) => /^dim\d+$/.test(k)
  );
  const hasComponents = "numerator" in answer.rows[0];
  const hasChange = "current" in answer.rows[0];
  // Each period against the one before it (k-07): the prior period, the change
  // and the percentage, with the still-accumulating period marked.
  const hasStep = "change" in answer.rows[0];
  if (!dimensionKeys.length && answer.rows.length === 1) return null;

  const number = (v) =>
    v === null || v === undefined
      ? "—"
      : typeof v === "number"
      ? v.toLocaleString(undefined, { maximumFractionDigits: 2 })
      : v;

  return (
    <div className="table-wrap">
      <table>
        <caption className="sr-only">{answer.columns?.at(-1)} — {answer.period_note}</caption>
        <thead>
          <tr>
            {dimensionKeys.map((k, i) => (
              <th key={k} scope="col">{answer.columns[i] || "group"}</th>
            ))}
            {hasChange && <th className="num">current</th>}
            {hasChange && <th className="num">prior</th>}
            {hasStep && <th className="num">prior period</th>}
            {hasStep && <th className="num">change</th>}
            {hasStep && <th className="num">% change</th>}
            {hasComponents && <th className="num">ours</th>}
            {hasComponents && <th className="num">market</th>}
            <th className="num">{answer.columns[answer.columns.length - 1]}</th>
          </tr>
        </thead>
        <tbody>
          {answer.rows.map((row, idx) => (
            <tr key={idx}>
              {dimensionKeys.map((k) => (
                <td key={k} title={row[`${k}_id`] || ""}>
                  {row[k]}
                </td>
              ))}
              {hasChange && <td className="num">{number(row.current)}</td>}
              {hasChange && <td className="num">{number(row.prior)}</td>}
              {hasStep && <td className="num">{number(row.prior)}</td>}
              {hasStep && <td className="num">{row.change_formatted}</td>}
              {hasStep && <td className="num">{row.change_pct_formatted}</td>}
              {hasComponents && <td className="num">{number(row.numerator)}</td>}
              {hasComponents && <td className="num">{number(row.denominator)}</td>}
              <td className="num strong">
                {row.value_formatted}
                {row.provisional && <span className="muted small"> (provisional)</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {answer.truncated && (
        <p className="muted small">
          Showing the first {answer.rows.length} rows of {answer.row_count}.
        </p>
      )}
    </div>
  );
}

const REASONS = [
  ["wrong_number", "Wrong number"],
  ["different_question", "Answered a different question"],
  ["missing_data", "Missing data"],
  ["other", "Something else"],
];

// Feedback on one saved answer. It goes to the server with the run id, and
// only the person who asked can give it; it does not change any answer.
function Feedback({ runId }) {
  const [state, setState] = useState("idle");
  const send = async (helpful, reason) => {
    setState("sending");
    try {
      await api("/api/feedback", {
        method: "POST",
        body: JSON.stringify({ run_id: runId, helpful, reason }),
      });
      setState("sent");
    } catch {
      setState("failed");
    }
  };
  if (state === "sent") return <p className="muted small">Thanks, noted.</p>;
  if (state === "failed") return <p className="muted small">Feedback could not be recorded.</p>;
  if (state === "why") {
    return (
      <div className="feedback" role="group" aria-label="What was wrong">
        {REASONS.map(([code, label]) => (
          <button key={code} className="link small" onClick={() => send(false, code)}>
            {label}
          </button>
        ))}
      </div>
    );
  }
  return (
    <div className="feedback" role="group" aria-label="Was this right">
      <button className="link small" disabled={state === "sending"} onClick={() => send(true)}>
        Helpful
      </button>
      <button className="link small" disabled={state === "sending"}
              onClick={() => setState("why")}>
        Not right
      </button>
    </div>
  );
}

function Turn({ turn, onChoose }) {
  if (turn.role === "user") {
    return (
      <div className="turn user">
        <div className="bubble">{turn.text}</div>
      </div>
    );
  }

  if (turn.pending) {
    return (
      <div className="turn assistant">
        <div className="bubble">
          <span className="thinking">
            <span /> <span /> <span />
          </span>{" "}
          {turn.stage}
        </div>
      </div>
    );
  }

  const answer = turn.answer;
  return (
    <div className="turn assistant">
      <div className={`bubble ${turn.status}`}>
        <p className="headline">{turn.text}</p>

        {turn.alternative && <p className="alternative">{turn.alternative}</p>}

        {/* Options shown exactly as the server stored them, in its order, so
            choosing the second one means what the server will read as 2. */}
        {turn.choices?.length > 0 && (
          <div className="choices" role="group" aria-label="Choose one">
            {turn.choices.map((c, i) => (
              <button
                key={c.id}
                className="choice"
                disabled={!onChoose}
                onClick={() => onChoose?.(String(i + 1), `${c.label} (${c.detail})`)}
              >
                <strong>{c.label}</strong> <span className="muted small">{c.detail}</span>
              </button>
            ))}
          </div>
        )}

        {turn.persistence === "failed" && (
          <p className="warning" role="status">
            This answer was not saved to the conversation, so a follow-up will not
            build on it.
          </p>
        )}

        {answer && <ResultChart answer={answer} />}
        {answer && <ResultTable answer={answer} />}

        {answer && (
          <div className="meta">
            <span>{answer.scope_note}</span>
            <span>{answer.period_note}</span>
          </div>
        )}

        {answer?.notes?.map((note, i) => (
          <p key={i} className="note">
            {note}
          </p>
        ))}

        {/* Business warnings are shown only when they change how the number
            should be read. */}
        {answer?.warnings?.map((warning, i) => (
          <p key={i} className="warning">
            {warning}
          </p>
        ))}

        {turn.sql && (
          <details className="sql">
            <summary>SQL that produced this answer</summary>
            <pre>{turn.sql}</pre>
          </details>
        )}

        {turn.status === "answered" && turn.persistence === "saved" && turn.runId && (
          <Feedback runId={turn.runId} />
        )}
      </div>
    </div>
  );
}

const SUGGESTIONS = [
  "What are my top 5 accounts by pack units this quarter?",
  "What is our market share for Zenovax?",
  "Show me the monthly volume trend for Gemtara over the last 6 months",
  "Compare Onmark vs ION affiliated accounts by total pack units",
];

// How current the answers are. The date a sale was last recorded, not the
// date the page loaded; publication and feed times on hover.
function Freshness({ dataset }) {
  if (!dataset) return null;
  const through = dataset.data_through || dataset.latest_month;
  const details = [
    dataset.published_at && `published ${new Date(dataset.published_at).toLocaleString()}`,
    dataset.last_ingest_at && `feed last delivered ${new Date(dataset.last_ingest_at).toLocaleString()}`,
  ].filter(Boolean).join(" · ");
  return (
    <span className="muted small freshness" title={details || undefined}>
      {" "}· {through ? `data through ${through}` : ""}
    </span>
  );
}

export default function App() {
  const [user, setUser] = useState(null);
  const [dataset, setDataset] = useState(null);
  const [turns, setTurns] = useState([]);
  const [question, setQuestion] = useState("");
  const [conversationId, setConversationId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [showSql, setShowSql] = useState(false);
  const [notice, setNotice] = useState(null);
  const bottom = useRef(null);
  // The key of the question in flight, so Stop can cancel it on the server
  // before its run id is known.
  const currentKey = useRef(null);

  // Every sign-in and sign-out bumps this. A request captures the value it was
  // issued under and refuses to touch state if it has moved on since.
  //
  // Without it, a slow /api/ask that resolves after the user signs out writes
  // its answer into the transcript anyway -- `setTurns` does not know the
  // identity changed -- and because signing in did not clear the transcript,
  // the next person to sign in on that browser saw the previous person's
  // answer, headline figures included.
  const identityEpoch = useRef(0);
  const inFlight = useRef(null);

  const newIdentity = () => {
    identityEpoch.current += 1;
    inFlight.current?.abort();
    inFlight.current = null;
    setTurns([]);
    setConversationId(null);
    setQuestion("");
    setBusy(false);
    return identityEpoch.current;
  };

  useEffect(() => {
    const epoch = identityEpoch.current;
    api("/api/me")
      .then(({ user, dataset }) => {
        if (identityEpoch.current !== epoch) return;
        setUser(user);
        setDataset(dataset);
      })
      .catch(() => {
        if (identityEpoch.current !== epoch) return;
        setUser(null);
      });
  }, []);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  const signIn = async (u) => {
    // Clear first, then adopt the new identity: a transcript belongs to the
    // session that produced it and must not survive into the next one.
    const epoch = newIdentity();
    setUser(u);
    const { dataset } = await api("/api/me");
    if (identityEpoch.current !== epoch) return;
    setDataset(dataset);
  };

  // Starting over without signing out. A conversation carries a plan and a
  // frozen cohort forward, so there was no way to ask an unrelated question
  // except to sign out and back in -- and the transcript is where a previous
  // answer's figures are still on screen.
  const newConversation = () => {
    inFlight.current?.abort();
    inFlight.current = null;
    setTurns([]);
    setConversationId(null);
    setQuestion("");
    setBusy(false);
  };

  // Deleting removes the conversation on the server -- its turns, the
  // populations a follow-up would refer to, and any paused question -- not
  // just this screen.
  const deleteConversation = async () => {
    if (!conversationId) return;
    if (!window.confirm("Delete this conversation? This cannot be undone.")) return;
    try {
      await api(`/api/conversations/${encodeURIComponent(conversationId)}`,
                { method: "DELETE" });
      newConversation();
    } catch (err) {
      window.alert(err.message);
    }
  };

  const signOut = async () => {
    newIdentity();
    setUser(null);
    setDataset(null);
    await api("/api/logout", { method: "POST" });
  };

  const replaceLast = (turn) => setTurns((t) => [...t.slice(0, -1), turn]);

  const ask = async (text, display) => {
    const q = (text ?? question).trim();
    if (!q || busy) return;
    // Captured now, checked before every write below. `stale()` is the only
    // thing standing between a slow answer and the wrong person's screen.
    const epoch = identityEpoch.current;
    const stale = () => identityEpoch.current !== epoch;

    const controller = new AbortController();
    inFlight.current = controller;
    const key = newKey();
    currentKey.current = key;

    setQuestion("");
    setBusy(true);
    setTurns((t) => [
      ...t,
      { role: "user", text: display || q },
      { role: "assistant", pending: true, stage: "Interpreting your question…" },
    ]);

    const stage = setTimeout(() => {
      if (stale()) return;
      setTurns((t) => {
        const copy = [...t];
        const last = copy[copy.length - 1];
        if (last?.pending) last.stage = "Querying the data you have access to…";
        return copy;
      });
    }, 500);

    const body = JSON.stringify({
      question: q,
      conversation_id: conversationId,
      include_sql: showSql,
    });
    const send = async (attempt) => {
      try {
        const res = await api("/api/ask", {
          method: "POST",
          signal: controller.signal,
          headers: { "Idempotency-Key": key },
          body,
        });
        // The data was republished mid-answer. The server closed the run
        // without an answer, so the same key runs again -- on the new data.
        if (res.status === "refresh" && attempt < 1 && !stale()) {
          await sleep(300);
          return send(attempt + 1);
        }
        return res;
      } catch (err) {
        if (err.name === "AbortError" || stale()) throw err;
        // Safe to resend with the SAME key: the server returns the outcome it
        // committed for this key rather than answering a second time. A
        // dropped connection has no status; a 5xx or "still running" is
        // worth one more look.
        const retryable =
          err.status === undefined || err.status >= 500 ||
          err.code === "same_request_running";
        if (retryable && attempt < 2) {
          // Overload: wait as long as the server asks (at most 10 s), with
          // jitter so that every refused browser does not come back at the
          // same instant and refuse itself again.
          const wait = err.code === "overloaded"
            ? Math.min(Number(err.retryAfter) || 2, 10) * 1000 * (1 + Math.random() * 0.5)
            : (attempt === 0 ? 400 : 1200);
          await sleep(wait);
          return send(attempt + 1);
        }
        throw err;
      }
    };

    try {
      const res = await send(0);
      if (stale()) return;
      setConversationId(res.conversation_id);
      replaceLast({
        role: "assistant",
        status: res.status,
        text: res.status === "cancelled" ? "Stopped." : res.message,
        alternative: res.alternative,
        answer: res.answer,
        sql: res.sql,
        choices: res.choices,
        persistence: res.persistence,
        runId: res.run_id,
      });
    } catch (err) {
      // An abort is this component cancelling its own request, not a failure
      // worth showing -- and after an identity change there is no transcript
      // it would belong to anyway.
      if (stale() || err.name === "AbortError") return;
      if (err.status === 401) {
        // Idle or absolute expiry, or revoked elsewhere: back to sign-in,
        // with the transcript cleared like any other identity change.
        newIdentity();
        setUser(null);
        setDataset(null);
        setNotice("Your session ended. Sign in again to continue.");
        return;
      }
      replaceLast({ role: "assistant", status: "error", text: err.message });
    } finally {
      clearTimeout(stage);
      if (inFlight.current === controller) inFlight.current = null;
      if (currentKey.current === key) currentKey.current = null;
      if (!stale()) setBusy(false);
    }
  };

  // Stop the question in flight: abort the request here, and ask the server
  // to stop its run at the next step, by the key -- the run id is not known
  // until a response arrives.
  const stop = () => {
    const key = currentKey.current;
    inFlight.current?.abort();
    inFlight.current = null;
    setBusy(false);
    replaceLast({ role: "assistant", status: "cancelled", text: "Stopped." });
    if (key) {
      api("/api/runs/cancel", {
        method: "POST",
        body: JSON.stringify({ idempotency_key: key }),
      }).catch(() => {});
    }
  };

  if (!user) return <Login onSignedIn={(u) => { setNotice(null); signIn(u); }} notice={notice} />;

  return (
    <div className="app">
      <header>
        <div>
          <strong>Pharma Analytics Copilot</strong>
          <Freshness dataset={dataset} />
        </div>
        <div className="identity">
          <span className="who">{user.name}</span>
          <span className="badge">{user.role}</span>
          <span className="muted small">{user.scope}</span>
          {!user.can_view_pricing && (
            <span className="badge quiet" title="Pricing is restricted for your role">
              no pricing
            </span>
          )}
          <button
            className="link"
            onClick={newConversation}
            disabled={turns.length === 0}
            title="Clear this thread and start an unrelated question"
          >
            New conversation
          </button>
          <button
            className="link"
            onClick={deleteConversation}
            disabled={!conversationId || busy}
            title="Delete this conversation from the server"
          >
            Delete conversation
          </button>
          <a className="link" href="/api/me/data" download="my-data.json"
             title="Download your conversations and account data">
            My data
          </a>
          <button className="link" onClick={signOut}>
            Sign out
          </button>
        </div>
      </header>

      <main>
        {turns.length === 0 && (
          <div className="empty">
            <h2>Ask about commercial performance</h2>
            <p className="muted">
              You are seeing {user.scope}. Answers state the metric, the reporting
              period and any data-quality caveats.
            </p>
            <div className="suggestions">
              {SUGGESTIONS.map((s) => (
                <button key={s} onClick={() => ask(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {turns.map((turn, i) => (
          <Turn
            key={i}
            turn={turn}
            // Only the latest clarification can be answered: an older one has
            // been superseded on the server.
            onChoose={!busy && i === turns.length - 1 ? ask : undefined}
          />
        ))}
        <div ref={bottom} />
      </main>

      <footer>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            ask();
          }}
        >
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="Ask a question, or follow up on the last answer…"
            disabled={busy}
            autoFocus
          />
          {busy ? (
            <button type="button" className="stop" onClick={stop}>
              Stop
            </button>
          ) : (
            <button type="submit" disabled={!question.trim()}>
              Ask
            </button>
          )}
        </form>
        <label className="sql-toggle">
          <input
            type="checkbox"
            checked={showSql}
            onChange={(e) => setShowSql(e.target.checked)}
          />
          Show the SQL behind answers
        </label>
      </footer>
    </div>
  );
}

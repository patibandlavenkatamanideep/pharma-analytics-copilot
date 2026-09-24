import { useEffect, useRef, useState } from "react";

const api = async (path, options = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    // `detail` is a plain string for expected refusals, and an object carrying
    // a request id for an unhandled failure. Showing the id gives the user
    // something to quote when reporting it.
    const detail = body.detail;
    const message =
      typeof detail === "string"
        ? detail
        : detail?.message || "Something went wrong.";
    const err = new Error(
      detail?.request_id ? `${message} (reference ${detail.request_id})` : message,
    );
    err.requestId = detail?.request_id;
    throw err;
  }
  return body;
};

function Login({ onSignedIn }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

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
        {error && <div className="error">{error}</div>}
        <button type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

function ResultTable({ answer }) {
  if (!answer?.rows?.length) return null;
  const dimensionKeys = Object.keys(answer.rows[0]).filter(
    (k) => /^dim\d+$/.test(k)
  );
  const hasComponents = "numerator" in answer.rows[0];
  const hasChange = "current" in answer.rows[0];
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
        <thead>
          <tr>
            {dimensionKeys.map((k, i) => (
              <th key={k}>{answer.columns[i] || "group"}</th>
            ))}
            {hasChange && <th className="num">current</th>}
            {hasChange && <th className="num">prior</th>}
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
              {hasComponents && <td className="num">{number(row.numerator)}</td>}
              {hasComponents && <td className="num">{number(row.denominator)}</td>}
              <td className="num strong">{row.value_formatted}</td>
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

function Turn({ turn }) {
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

export default function App() {
  const [user, setUser] = useState(null);
  const [dataset, setDataset] = useState(null);
  const [turns, setTurns] = useState([]);
  const [question, setQuestion] = useState("");
  const [conversationId, setConversationId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [showSql, setShowSql] = useState(false);
  const bottom = useRef(null);

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

  const signOut = async () => {
    newIdentity();
    setUser(null);
    setDataset(null);
    await api("/api/logout", { method: "POST" });
  };

  const ask = async (text) => {
    const q = (text ?? question).trim();
    if (!q || busy) return;
    // Captured now, checked before every write below. `stale()` is the only
    // thing standing between a slow answer and the wrong person's screen.
    const epoch = identityEpoch.current;
    const stale = () => identityEpoch.current !== epoch;

    const controller = new AbortController();
    inFlight.current = controller;

    setQuestion("");
    setBusy(true);
    setTurns((t) => [
      ...t,
      { role: "user", text: q },
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

    try {
      const res = await api("/api/ask", {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({
          question: q,
          conversation_id: conversationId,
          include_sql: showSql,
        }),
      });
      if (stale()) return;
      setConversationId(res.conversation_id);
      setTurns((t) => [
        ...t.slice(0, -1),
        {
          role: "assistant",
          status: res.status,
          text: res.message,
          alternative: res.alternative,
          answer: res.answer,
          sql: res.sql,
        },
      ]);
    } catch (err) {
      // An abort is this component cancelling its own request, not a failure
      // worth showing -- and after an identity change there is no transcript
      // it would belong to anyway.
      if (stale() || err.name === "AbortError") return;
      setTurns((t) => [
        ...t.slice(0, -1),
        { role: "assistant", status: "error", text: err.message },
      ]);
    } finally {
      clearTimeout(stage);
      if (inFlight.current === controller) inFlight.current = null;
      if (!stale()) setBusy(false);
    }
  };

  if (!user) return <Login onSignedIn={signIn} />;

  return (
    <div className="app">
      <header>
        <div>
          <strong>Pharma Analytics Copilot</strong>
          <span className="muted small">
            {" "}
            · {dataset?.latest_month ? `data through ${dataset.latest_month}` : ""}
          </span>
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
          <Turn key={i} turn={turn} />
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
          <button type="submit" disabled={busy || !question.trim()}>
            Ask
          </button>
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

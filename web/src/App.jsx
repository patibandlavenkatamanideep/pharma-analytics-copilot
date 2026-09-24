import { useEffect, useRef, useState } from "react";

const api = async (path, options = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || "Something went wrong.");
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

  useEffect(() => {
    api("/api/me")
      .then(({ user, dataset }) => {
        setUser(user);
        setDataset(dataset);
      })
      .catch(() => setUser(null));
  }, []);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  const signIn = async (u) => {
    setUser(u);
    const { dataset } = await api("/api/me");
    setDataset(dataset);
  };

  const signOut = async () => {
    await api("/api/logout", { method: "POST" });
    setUser(null);
    setTurns([]);
    setConversationId(null);
  };

  const ask = async (text) => {
    const q = (text ?? question).trim();
    if (!q || busy) return;
    setQuestion("");
    setBusy(true);
    setTurns((t) => [
      ...t,
      { role: "user", text: q },
      { role: "assistant", pending: true, stage: "Interpreting your question…" },
    ]);

    const stage = setTimeout(() => {
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
        body: JSON.stringify({
          question: q,
          conversation_id: conversationId,
          include_sql: showSql,
        }),
      });
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
      setTurns((t) => [
        ...t.slice(0, -1),
        { role: "assistant", status: "error", text: err.message },
      ]);
    } finally {
      clearTimeout(stage);
      setBusy(false);
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

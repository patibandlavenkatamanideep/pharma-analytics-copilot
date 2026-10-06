import { useId } from "react";

// This view has no fetch path. Its only data is the already-authorized table.
// Mixed grains and scalar results stay in the table/headline; no aggregation
// or invented zero is needed to make them fit a chart.
export default function ResultChart({ answer }) {
  const captionId = useId();
  if (answer?.dimensions?.length !== 1 || !answer.unit || answer.rows?.length < 2) return null;
  const isTime = ["period_mo", "period_qtr", "period_wk"].includes(answer.dimensions[0]);
  const all = [...(answer.rows || [])];
  if (isTime) all.sort((a, b) => String(a.dim0).localeCompare(String(b.dim0)));
  const rows = all.slice(0, 12);
  const values = rows.map(r => r.value).filter(v => typeof v === "number" && Number.isFinite(v));
  if (!values.length) return null;
  const low = Math.min(0, ...values), high = Math.max(0, ...values);
  const range = high - low || 1;
  const position = value => (value - low) / range * 100;
  const zero = position(0);
  const unit = answer.unit === "ratio" ? "%" : answer.unit;
  const metric = answer.columns?.at(-1) || "Value";

  return <figure className="result-chart" aria-labelledby={captionId}>
    <figcaption id={captionId}>
      <strong>{metric} · {unit}</strong>
      <span className="muted small">{answer.period_note}</span>
      <span className="muted small">{answer.data_through
        ? `Data through ${answer.data_through}` : "Data freshness unavailable"}</span>
    </figcaption>
    <ol className="chart-rows" aria-label={isTime ? "Values by reporting period" : "Group comparison"}>
      {rows.map((row, index) => {
        const known = typeof row.value === "number" && Number.isFinite(row.value);
        const end = known ? position(row.value) : zero;
        return <li key={index}>
          <div className="chart-label"><span>{row.dim0}</span>
            <strong>{known ? row.value_formatted : "unavailable"}</strong></div>
          <svg viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true">
            <line x1={zero} x2={zero} y1="0" y2="10" className="chart-zero" />
            {known && <rect x={Math.min(zero, end)} y="2" width={Math.abs(end - zero)}
              height="6" className={row.value < 0 ? "chart-negative" : "chart-positive"} />}
          </svg>
        </li>;
      })}
    </ol>
    <p className="muted small">Bars share a zero baseline. Missing values are unavailable.
      {all.length > rows.length && ` Chart shows the first ${rows.length} of ${all.length} returned rows.`}
      {answer.truncated && " The query result is truncated."} Full returned values are in the table below.</p>
  </figure>;
}

import { useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import './App.css'

type ChatRequest = {
  prompt: string
  session_id?: string
  model?: string
  table_pattern: string
  database?: string
  schema_name?: string
}

type ChatResponse = {
  session_id: string | null
  model: string | null
  question: string
  generated_sql: string
  snowflake_data: Array<Record<string, unknown>>
  row_count: number
  ai_response: string
  dialect: string
  timestamp: string
  success: boolean
  error: string | null
}

function App() {
  const [form, setForm] = useState<ChatRequest>({
    prompt: '',
    session_id: 'session-001',
    table_pattern: '%',
    database: '',
    schema_name: '',
  })
  const [data, setData] = useState<ChatResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const preview = useMemo(
    () => (data?.snowflake_data ?? []).slice(0, 8),
    [data?.snowflake_data],
  )

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setLoading(true)
    setError(null)

    try {
      const payload: ChatRequest = {
        ...form,
        database: form.database || undefined,
        schema_name: form.schema_name || undefined,
      }

      const response = await fetch('/api/v1/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })

      const json = (await response.json()) as ChatResponse | { detail?: string }
      if (!response.ok) {
        const detail = 'detail' in json && json.detail ? json.detail : 'Request failed'
        throw new Error(detail)
      }

      setData(json as ChatResponse)
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'Unexpected error'
      setError(message)
      setData(null)
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="app-shell">
      <header className="hero">
        <p className="eyebrow">Enterprise AI Analytics Platform</p>
        <h1>Ask data questions in plain English</h1>
        <p className="lead">
          The assistant converts your prompt into safe Snowflake SQL, executes it, and returns
          results with a business explanation.
        </p>
      </header>

      <section className="panel">
        <form onSubmit={onSubmit} className="query-form">
          <label htmlFor="prompt">Question</label>
          <textarea
            id="prompt"
            value={form.prompt}
            onChange={(event) => setForm((current) => ({ ...current, prompt: event.target.value }))}
            placeholder="Example: Show top 10 customers by revenue in the last 30 days"
            required
          />

          <div className="grid-3">
            <div>
              <label htmlFor="session_id">Session ID</label>
              <input
                id="session_id"
                value={form.session_id ?? ''}
                onChange={(event) =>
                  setForm((current) => ({ ...current, session_id: event.target.value || undefined }))
                }
              />
            </div>

            <div>
              <label htmlFor="database">Database</label>
              <input
                id="database"
                value={form.database ?? ''}
                onChange={(event) => setForm((current) => ({ ...current, database: event.target.value }))}
              />
            </div>

            <div>
              <label htmlFor="schema_name">Schema</label>
              <input
                id="schema_name"
                value={form.schema_name ?? ''}
                onChange={(event) =>
                  setForm((current) => ({ ...current, schema_name: event.target.value }))
                }
              />
            </div>
          </div>

          <div className="grid-2">
            <div>
              <label htmlFor="table_pattern">Table Pattern</label>
              <input
                id="table_pattern"
                value={form.table_pattern}
                onChange={(event) =>
                  setForm((current) => ({ ...current, table_pattern: event.target.value }))
                }
              />
            </div>

            <div>
              <label htmlFor="model">Model</label>
              <input
                id="model"
                value={form.model ?? ''}
                onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))}
                placeholder="llama-3.3-70b-versatile"
              />
            </div>
          </div>

          <button type="submit" className="cta" disabled={loading}>
            {loading ? 'Generating answer...' : 'Run AI Analysis'}
          </button>
        </form>
      </section>

      {error ? <section className="panel error">{error}</section> : null}

      {data ? (
        <section className="panel output">
          <div className="meta-row">
            <span>Status: {data.success ? 'Success' : 'Failed'}</span>
            <span>Rows: {data.row_count}</span>
            <span>Dialect: {data.dialect}</span>
          </div>

          <h2>Generated SQL</h2>
          <pre>{data.generated_sql}</pre>

          <h2>Business Explanation</h2>
          <p>{data.ai_response || 'No explanation available.'}</p>

          <h2>Snowflake Data Preview</h2>
          <pre>{JSON.stringify(preview, null, 2)}</pre>
        </section>
      ) : (
        <section className="panel empty">
          <p>Submit a question to view SQL generation, query results, and AI explanation.</p>
        </section>
      )}
    </main>
  )
}

export default App

import { KeyboardEvent, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import "./App.css";

type Page = "chat" | "usage" | "devices" | "memory" | "settings";

type ToolResult = Record<string, unknown>;

type Message = {
  id: number;
  role: "user" | "assistant";
  text: string;
  toolResults?: ToolResult[];
};

type ConnectionState = "connecting" | "online" | "offline";

type ServerConfig = {
  url: string;
  token: string;
};

type DeviceSnapshot = {
  device_id: string;
  display_name: string;
  platform: string;
  capabilities: string[];
  online: boolean;
  last_seen: string;
  sensitive_state_local: boolean;
};

type UsageTotals = {
  requests: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  estimated_cost: number | null;
  cost_currency: string | null;
  cache_hit_percent: number | null;
};

type UsageModel = UsageTotals & {
  model: string;
  request_kind: string;
};

type UsageCall = {
  timestamp: string;
  request_kind: string;
  model: string;
  input_tokens: number | null;
  cached_input_tokens: number | null;
  output_tokens: number | null;
  reasoning_tokens: number | null;
  latency_ms: number | null;
  estimated_cost: number | null;
};

type UsageHour = {
  start_time: string;
  end_time: string | null;
  requests: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  total_tokens: number;
};

type ToolUsageOperation = {
  provider: string;
  operation: string;
  capability: string;
  calls: number;
  succeeded: number;
  failed: number;
  unknown: number;
  api_requests: number;
  average_latency_ms: number | null;
  estimated_cost: number | null;
  currency: string | null;
};

type ToolUsage = {
  totals: {
    calls: number;
    succeeded: number;
    failed: number;
    unknown: number;
    api_requests: number;
    estimated_cost: number | null;
  };
  by_operation: ToolUsageOperation[];
};

type UsageSnapshot = {
  source: "openai" | "local";
  window: { hours: number; label?: string; from: string; to: string; timezone?: string };
  totals: UsageTotals;
  by_model: UsageModel[];
  hourly: UsageHour[];
  recent_calls: UsageCall[];
  tool_usage?: ToolUsage;
  warning: string | null;
};

const CHAT_STORAGE_KEY = "epis.desktop.chat.v1";

let messageId = 0;

function nextMessageId() {
  messageId += 1;
  return messageId;
}

function asObject(value: unknown): ToolResult | null {
  if (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value)
  ) {
    return value as ToolResult;
  }

  return null;
}

function loadStoredMessages(): Message[] {
  try {
    const raw = window.localStorage.getItem(CHAT_STORAGE_KEY);

    if (!raw) {
      return [];
    }

    const parsed: unknown = JSON.parse(raw);

    if (!Array.isArray(parsed)) {
      return [];
    }

    const restored: Message[] = [];

    for (const value of parsed) {
      const item = asObject(value);

      if (item === null) {
        continue;
      }

      if (
        item.role !== "user" &&
        item.role !== "assistant"
      ) {
        continue;
      }

      if (typeof item.text !== "string") {
        continue;
      }

      const toolResults = Array.isArray(item.toolResults)
        ? item.toolResults
            .map(asObject)
            .filter(
              (tool): tool is ToolResult =>
                tool !== null,
            )
        : undefined;

      restored.push({
        id: nextMessageId(),
        role: item.role,
        text: item.text,
        toolResults,
      });
    }

    return restored;
  } catch {
    return [];
  }
}

function toolTitle(tool: ToolResult): string {
  const candidates = [
    tool.tool,
    tool.name,
    tool.action,
    tool.capability,
    tool.type,
  ];

  for (const value of candidates) {
    if (typeof value === "string" && value.trim()) {
      return value;
    }
  }

  return "Ara\u00e7 i\u015flemi";
}

function toolSucceeded(tool: ToolResult): boolean | null {
  if (typeof tool.ok === "boolean") {
    return tool.ok;
  }

  if (typeof tool.success === "boolean") {
    return tool.success;
  }

  if (typeof tool.error === "string" && tool.error) {
    return false;
  }

  return null;
}

function toolSummary(tool: ToolResult): string {
  const candidates = [
    tool.summary,
    tool.message,
    tool.path,
    tool.result,
    tool.error,
  ];

  for (const value of candidates) {
    if (typeof value === "string" && value.trim()) {
      return value;
    }
  }

  return "Ayr\u0131nt\u0131lar\u0131 g\u00f6r\u00fcnt\u00fcle";
}

function asUsageSnapshot(value: unknown): UsageSnapshot | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }

  const data = value as Record<string, unknown>;
  const totals = data.totals;

  if (
    totals === null ||
    typeof totals !== "object" ||
    Array.isArray(totals) ||
    !Array.isArray(data.by_model) ||
    !Array.isArray(data.hourly) ||
    !Array.isArray(data.recent_calls)
  ) {
    return null;
  }

  return data as unknown as UsageSnapshot;
}

function asDeviceSnapshots(value: unknown): DeviceSnapshot[] | null {
  if (!Array.isArray(value)) return null;
  const devices: DeviceSnapshot[] = [];
  for (const item of value) {
    if (item === null || typeof item !== "object" || Array.isArray(item)) return null;
    const device = item as Record<string, unknown>;
    if (
      typeof device.device_id !== "string" ||
      typeof device.display_name !== "string" ||
      typeof device.platform !== "string" ||
      typeof device.online !== "boolean" ||
      !Array.isArray(device.capabilities)
    ) return null;
    devices.push(device as unknown as DeviceSnapshot);
  }
  return devices;
}

function formatNumber(value: number | null | undefined): string {
  return typeof value === "number"
    ? new Intl.NumberFormat("tr-TR").format(value)
    : "—";
}

function formatLatency(value: number | null): string {
  return typeof value === "number" ? `${(value / 1000).toFixed(1)}s` : "—";
}

function formatCost(value: number | null, currency: string | null): string {
  if (value === null) {
    return "—";
  }

  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: (currency ?? "usd").toUpperCase(),
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(value);
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : new Intl.DateTimeFormat("tr-TR", {
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
}

function modelLabel(requestKind: string): string {
  if (requestKind === "luna") return "Luna";
  if (requestKind === "sol") return "Sol";
  return "Model";
}

function providerLabel(provider: string): string {
  if (provider === "local-device") return "Windows";
  return provider.charAt(0).toUpperCase() + provider.slice(1);
}

function callTokenTotal(call: UsageCall): number | null {
  if (call.input_tokens === null && call.output_tokens === null) {
    return null;
  }
  return (call.input_tokens ?? 0) + (call.output_tokens ?? 0);
}

function barHeight(hourly: UsageHour[], value: number): string {
  const peak = Math.max(0, ...hourly.map((item) => item.total_tokens));
  if (peak === 0 || value === 0) return "3px";
  return `${Math.max(8, (value / peak) * 100)}%`;
}

export default function App() {
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const [page, setPage] = useState<Page>("chat");
  const [connection, setConnection] =
    useState<ConnectionState>("connecting");

  const [serverVersion, setServerVersion] = useState("");
  const [serverConfig, setServerConfig] = useState<ServerConfig | null>(null);
  const [messages, setMessages] =
    useState<Message[]>(loadStoredMessages);
  const [text, setText] = useState("");
  const [waiting, setWaiting] = useState(false);
  const [confirmationPending, setConfirmationPending] =
    useState(false);
  const [usage, setUsage] = useState<UsageSnapshot | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageError, setUsageError] = useState<string | null>(null);
  const [devices, setDevices] = useState<DeviceSnapshot[]>([]);
  const [devicesLoading, setDevicesLoading] = useState(false);
  const [devicesError, setDevicesError] = useState<string | null>(null);

  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    try {
      window.localStorage.setItem(
        CHAT_STORAGE_KEY,
        JSON.stringify(
          messages.map(
            ({ role, text, toolResults }) => ({
              role,
              text,
              toolResults,
            }),
          ),
        ),
      );
    } catch {
      // UI persistence failure must not break chat.
    }
  }, [messages]);

  useEffect(() => {
    invoke<ServerConfig>("server_config")
      .then(setServerConfig)
      .catch(() => {
        setServerConfig({
          url: "ws://127.0.0.1:8000/ws",
          token: "",
        });
      });
  }, []);

  useEffect(() => {
    if (serverConfig === null) {
      return;
    }
    const activeServer = serverConfig;

    let disposed = false;

    function connect() {
      if (disposed) {
        return;
      }

      setConnection("connecting");

      const ws = activeServer.token
        ? new WebSocket(activeServer.url, ["epis", activeServer.token])
        : new WebSocket(activeServer.url);
      socketRef.current = ws;

      ws.onopen = () => {
        if (disposed || socketRef.current !== ws) {
          return;
        }

        setConnection("online");
        setError(null);
      };

      ws.onmessage = (event) => {
        if (disposed || socketRef.current !== ws) {
          return;
        }

        try {
          const data = JSON.parse(event.data);

          if (data.type === "connected") {
            setConnection("online");
            setServerVersion(data.version ?? "");
            return;
          }

          if (data.type === "assistant.message") {
            const rawResults: unknown[] = Array.isArray(data.tool_results)
              ? data.tool_results
              : [];

            const parsedResults = rawResults
              .map(asObject)
              .filter((item): item is ToolResult => item !== null);

            setMessages((current) => [
              ...current,
              {
                id: nextMessageId(),
                role: "assistant",
                text: String(data.text ?? ""),
                toolResults: parsedResults,
              },
            ]);

            setWaiting(false);
            setConfirmationPending(
              Boolean(data.confirmation_required),
            );
            return;
          }

          if (data.type === "usage.snapshot") {
            const snapshot = asUsageSnapshot(data.data);

            if (snapshot === null) {
              setUsageError("Usage verisi okunamadı.");
            } else {
              setUsage(snapshot);
              setUsageError(null);
            }

            setUsageLoading(false);
            return;
          }

          if (data.type === "devices.snapshot") {
            const snapshot = asDeviceSnapshots(data.devices);
            if (snapshot === null) {
              setDevicesError("Cihaz verisi okunamadı.");
            } else {
              setDevices(snapshot);
              setDevicesError(null);
            }
            setDevicesLoading(false);
            return;
          }

          if (data.type === "conversation.reset") {
            setMessages([]);
            setWaiting(false);
            setConfirmationPending(false);
            setError(null);
            return;
          }

          if (data.type === "error") {
            setWaiting(false);

            setError(
              data.detail ??
                data.error ??
                "Bilinmeyen server hatas\u0131.",
            );

            return;
          }
        } catch {
          setWaiting(false);
          setError(
            "Sunucudan ge\u00e7ersiz bir mesaj geldi.",
          );
        }
      };

      ws.onerror = () => {
        if (disposed || socketRef.current !== ws) {
          return;
        }

        setConnection("offline");
      };

      ws.onclose = () => {
        if (disposed || socketRef.current !== ws) {
          return;
        }

        socketRef.current = null;
        setConnection("offline");
        setWaiting(false);

        reconnectTimerRef.current = window.setTimeout(
          connect,
          1500,
        );
      };
    }

    connect();

    return () => {
      disposed = true;

      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
      }

      const ws = socketRef.current;

      if (ws) {
        socketRef.current = null;
        ws.close();
      }
    };
  }, [serverConfig]);

  useEffect(() => {
    if (page !== "usage" || connection !== "online") {
      return;
    }

    const ws = socketRef.current;

    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return;
    }

    setUsageLoading(true);
    setUsageError(null);
    ws.send(JSON.stringify({ type: "usage.get", hours: 24, limit: 20 }));
  }, [page, connection]);

  useEffect(() => {
    if (page !== "devices" || connection !== "online") return;
    const ws = socketRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    setDevicesLoading(true);
    setDevicesError(null);
    ws.send(JSON.stringify({ type: "devices.get" }));
  }, [page, connection]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "end",
    });
  }, [messages, waiting, confirmationPending]);

  function sendPacket(packet: object) {
    const ws = socketRef.current;

    if (!ws || ws.readyState !== WebSocket.OPEN) {
      setConnection("offline");
      setError(
        "EPIS Server'a ba\u011fl\u0131 de\u011fil.",
      );
      return false;
    }

    ws.send(JSON.stringify(packet));
    return true;
  }

  function send() {
    const value = text.trim();

    if (
      !value ||
      waiting ||
      confirmationPending
    ) {
      return;
    }

    if (
      !sendPacket({
        type: "chat.send",
        text: value,
      })
    ) {
      return;
    }

    setMessages((current) => [
      ...current,
      {
        id: nextMessageId(),
        role: "user",
        text: value,
      },
    ]);

    setText("");
    setWaiting(true);
    setError(null);
  }

  function handleKeyDown(
    event: KeyboardEvent<HTMLTextAreaElement>,
  ) {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      send();
    }
  }

  function approve() {
    if (
      waiting ||
      !sendPacket({
        type: "approval.confirm",
      })
    ) {
      return;
    }

    setConfirmationPending(false);
    setWaiting(true);
    setError(null);
  }

  function reject() {
    if (
      waiting ||
      !sendPacket({
        type: "approval.reject",
      })
    ) {
      return;
    }

    setConfirmationPending(false);
    setWaiting(true);
    setError(null);
  }

  function clearContext() {
    if (waiting) {
      return;
    }

    if (
      !sendPacket({
        type: "conversation.new",
      })
    ) {
      return;
    }

    setWaiting(true);
    setConfirmationPending(false);
    setError(null);
  }

  const empty = messages.length === 0;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="logo">E</div>

          <div>
            <strong>EPIS</strong>
            <span>Personal Intelligence System</span>
          </div>
        </div>

        <nav className="navigation">
          <button
            className={page === "chat" ? "active" : ""}
            onClick={() => setPage("chat")}
          >
            Chat
          </button>

          <button
            className={page === "usage" ? "active" : ""}
            onClick={() => setPage("usage")}
          >
            Usage
          </button>

          <button
            className={page === "devices" ? "active" : ""}
            onClick={() => setPage("devices")}
          >
            Devices
          </button>

          <button
            className={page === "memory" ? "active" : ""}
            onClick={() => setPage("memory")}
          >
            Memory
          </button>

          <button
            className={page === "settings" ? "active" : ""}
            onClick={() => setPage("settings")}
          >
            Settings
          </button>
        </nav>

        <div className="status-area">
          <div className="model-pill">
            <span className="dot green" />
            Luna
            <span className="divider" />
            <span className="dot dim" />
            Sol
          </div>

          <div className={`server-pill ${connection}`}>
            <span className="dot" />

            {connection === "online"
              ? `Server ${serverVersion || ""}`
              : connection === "connecting"
                ? "Ba\u011flan\u0131yor"
                : "Offline"}
          </div>
        </div>
      </header>

      {page === "chat" ? (
        <>
          <main className={`chat ${empty ? "empty" : ""}`}>
            {empty ? (
              <section className="welcome">
                <div className="orb">E</div>

                <h1>Selam, Emir.</h1>

                <p>
                  {"Bug\u00fcn ne yap\u0131yoruz?"}
                </p>
              </section>
            ) : (
              <div className="messages">
                {messages.map((message) => (
                  <article
                    key={message.id}
                    className={`message ${message.role}`}
                  >
                    <div className="role">
                      {message.role === "user"
                        ? "Sen"
                        : "EPIS"}
                    </div>

                    <div className="message-text">
                      {message.text}
                    </div>

                    {message.toolResults &&
                      message.toolResults.length > 0 && (
                        <div className="tool-stack">
                          {message.toolResults.map(
                            (tool, index) => {
                              const succeeded =
                                toolSucceeded(tool);

                              return (
                                <details
                                  className="tool-card"
                                  key={index}
                                >
                                  <summary>
                                    <span
                                      className={
                                        succeeded === false
                                          ? "tool-state failed"
                                          : succeeded === true
                                            ? "tool-state success"
                                            : "tool-state neutral"
                                      }
                                    />

                                    <span className="tool-name">
                                      {toolTitle(tool)}
                                    </span>

                                    <span className="tool-summary">
                                      {toolSummary(tool)}
                                    </span>

                                    <span className="tool-chevron">
                                      ?
                                    </span>
                                  </summary>

                                  <pre>
                                    {JSON.stringify(
                                      tool,
                                      null,
                                      2,
                                    )}
                                  </pre>
                                </details>
                              );
                            },
                          )}
                        </div>
                      )}
                  </article>
                ))}

                {waiting && (
                  <article className="message assistant">
                    <div className="role">EPIS</div>

                    <div className="activity-line">
                      <span className="activity-spinner" />

                      <span>
                        {"EPIS \u00e7al\u0131\u015f\u0131yor"}
                      </span>
                    </div>
                  </article>
                )}

                {confirmationPending && (
                  <section className="permission-card">
                    <div className="permission-icon">
                      !
                    </div>

                    <div className="permission-copy">
                      <strong>
                        {"\u0130zin gerekiyor"}
                      </strong>

                      <span>
                        {
                          "EPIS bu i\u015fleme devam etmek i\u00e7in onay\u0131n\u0131 bekliyor."
                        }
                      </span>
                    </div>

                    <div className="permission-actions">
                      <button
                        className="secondary"
                        onClick={reject}
                      >
                        Reddet
                      </button>

                      <button
                        className="primary"
                        onClick={approve}
                      >
                        {"\u0130zin ver"}
                      </button>
                    </div>
                  </section>
                )}

                <div ref={bottomRef} />
              </div>
            )}
          </main>

          <footer className="composer-zone">
            <div className="chat-actions">
              <button
                onClick={clearContext}
                disabled={
                  waiting ||
                  connection !== "online"
                }
              >
                {"Ba\u011flam\u0131 temizle"}
              </button>
            </div>

            {error && (
              <div className="error">
                <span>{error}</span>

                <button onClick={() => setError(null)}>
                  ?
                </button>
              </div>
            )}

            <form
              className="composer"
              onSubmit={(event) => {
                event.preventDefault();
                send();
              }}
            >
              <button
                className="attach"
                type="button"
                disabled
                title="Dosya ekleme daha sonra"
              >
                +
              </button>

              <textarea
                value={text}
                disabled={
                  connection !== "online" ||
                  confirmationPending
                }
                placeholder={
                  connection !== "online"
                    ? "Server ba\u011flant\u0131s\u0131 bekleniyor..."
                    : confirmationPending
                      ? "\u00d6nce izin iste\u011fini yan\u0131tla"
                      : "EPIS'e bir \u015fey s\u00f6yle..."
                }
                onChange={(event) =>
                  setText(event.target.value)
                }
                onKeyDown={handleKeyDown}
              />

              <button
                className="send"
                type="submit"
                disabled={
                  !text.trim() ||
                  waiting ||
                  confirmationPending ||
                  connection !== "online"
                }
              >
                {"\u2191"}
              </button>
            </form>
          </footer>
        </>
      ) : page === "usage" ? (
        <main className="panel-page">
          <section className="panel usage-panel">
            <div className="panel-kicker">USAGE · TODAY</div>
            <h1>API Usage</h1>
            <p>
              Toplamlar OpenAI Organization Usage ve Costs API'lerinden gelir.
              Prompt ve yanıt metni kaydedilmez.
            </p>

            {usageLoading && <div className="usage-state">Yükleniyor...</div>}

            {usageError && <div className="usage-state error-state">{usageError}</div>}

            {!usageLoading && !usageError && usage?.warning && (
              <div className="usage-state warning-state">{usage.warning}</div>
            )}

            {!usageLoading && !usageError && usage && usage.totals.requests === 0 &&
              (usage.tool_usage?.totals.calls ?? 0) === 0 && (
              <div className="usage-state">
                Bu zaman aralığında kaydedilmiş model veya araç çağrısı yok.
              </div>
            )}

            {!usageLoading && !usageError && usage &&
              (usage.totals.requests > 0 || (usage.tool_usage?.totals.calls ?? 0) > 0) && (
              <>
                <div className="usage-summary">
                  <div className="usage-card">
                    <span>Total tokens</span>
                    <strong>{formatNumber(usage.totals.total_tokens)}</strong>
                  </div>
                  <div className="usage-card">
                    <span>Requests</span>
                    <strong>{formatNumber(usage.totals.requests)}</strong>
                  </div>
                  <div className="usage-card">
                    <span>Estimated spend</span>
                    <strong>{formatCost(usage.totals.estimated_cost, usage.totals.cost_currency)}</strong>
                  </div>
                  <div className="usage-card">
                    <span>Cache hit</span>
                    <strong>
                      {usage.totals.cache_hit_percent === null
                        ? "—"
                        : `%${usage.totals.cache_hit_percent}`}
                    </strong>
                  </div>
                </div>

                <section className="usage-section">
                  <div className="usage-section-heading">
                    <h2>Models</h2>
                    <span>{usage.source === "openai" ? "OpenAI organization data" : "Yerel metadata"}</span>
                  </div>
                  <div className="usage-model-list">
                    {usage.by_model.map((item) => (
                      <article className="usage-model" key={`${item.request_kind}:${item.model}`}>
                        <div>
                          <strong>{modelLabel(item.request_kind)}</strong>
                          <span>{item.model}</span>
                        </div>
                        <dl>
                          <div><dt>Input</dt><dd>{formatNumber(item.input_tokens)}</dd></div>
                          <div><dt>Cached</dt><dd>{formatNumber(item.cached_input_tokens)}</dd></div>
                          <div><dt>Output</dt><dd>{formatNumber(item.output_tokens)}</dd></div>
                          {item.reasoning_tokens > 0 && (
                            <div><dt>Reasoning</dt><dd>{formatNumber(item.reasoning_tokens)}</dd></div>
                          )}
                        </dl>
                      </article>
                    ))}
                  </div>
                </section>

                <section className="usage-section">
                  <div className="usage-section-heading">
                    <h2>Last 24h token graph</h2>
                    <span>{usage.hourly.length} hourly buckets</span>
                  </div>
                  <div className="usage-chart" aria-label="Son 24 saatte kullanılan token miktarı">
                    {usage.hourly.length > 0 ? (
                      <div className="usage-bars">
                        {usage.hourly.map((hour) => (
                          <div
                            className="usage-bar-slot"
                            key={hour.start_time}
                            title={`${formatTimestamp(hour.start_time)} · ${formatNumber(hour.total_tokens)} tokens`}
                          >
                            <div
                              className="usage-bar"
                              style={{ height: barHeight(usage.hourly, hour.total_tokens) }}
                            />
                          </div>
                        ))}
                      </div>
                    ) : (
                      <span className="usage-chart-empty">Henüz saatlik OpenAI verisi yok.</span>
                    )}
                  </div>
                </section>

                <section className="usage-section">
                  <div className="usage-section-heading">
                    <h2>Recent calls</h2>
                    <span>Son {usage.recent_calls.length} çağrı</span>
                  </div>
                  <div className="usage-call-list">
                    {usage.recent_calls.map((call) => (
                      <article className="usage-call" key={`${call.timestamp}:${call.request_kind}:${call.model}`}>
                        <span>{formatTimestamp(call.timestamp)}</span>
                        <strong>{modelLabel(call.request_kind)}</strong>
                        <span>{call.model}</span>
                        <span>{formatNumber(callTokenTotal(call))} tokens</span>
                        <span>{formatLatency(call.latency_ms)}</span>
                      </article>
                    ))}
                  </div>
                </section>

                {usage.tool_usage && usage.tool_usage.totals.calls > 0 && (
                  <section className="usage-section">
                    <div className="usage-section-heading">
                      <h2>Integrations & local tools</h2>
                      <span>
                        {formatNumber(usage.tool_usage.totals.calls)} çağrı · {formatNumber(usage.tool_usage.totals.api_requests)} API isteği
                      </span>
                    </div>
                    <div className="usage-integration-list">
                      {usage.tool_usage.by_operation.map((item) => (
                        <article
                          className="usage-integration"
                          key={`${item.provider}:${item.operation}:${item.capability}`}
                        >
                          <div>
                            <strong>{providerLabel(item.provider)}</strong>
                            <span>{item.operation}</span>
                          </div>
                          <dl>
                            <div><dt>Calls</dt><dd>{formatNumber(item.calls)}</dd></div>
                            <div><dt>Success</dt><dd>{formatNumber(item.succeeded)}</dd></div>
                            <div><dt>API requests</dt><dd>{formatNumber(item.api_requests)}</dd></div>
                            <div><dt>Avg latency</dt><dd>{formatLatency(item.average_latency_ms)}</dd></div>
                            <div><dt>Cost</dt><dd>{formatCost(item.estimated_cost, item.currency)}</dd></div>
                          </dl>
                        </article>
                      ))}
                    </div>
                  </section>
                )}
              </>
            )}
          </section>
        </main>
      ) : page === "devices" ? (
        <main className="panel-page">
          <section className="panel">
            <div className="panel-kicker">DEVICES · LIVE</div>
            <h1>Devices</h1>
            <p>
              EPIS'e bağlı cihazlar ve izinli yetenekleri. Hassas cihaz durumu yerelde kalır.
            </p>

            {devicesLoading && <div className="usage-state">Yükleniyor...</div>}
            {devicesError && <div className="usage-state error-state">{devicesError}</div>}
            {!devicesLoading && !devicesError && devices.length === 0 && (
              <div className="usage-state">
                Bağlı cihaz yok. Desktop uygulamasını yeniden başlattığında Windows ajanı otomatik bağlanır.
              </div>
            )}
            {!devicesLoading && !devicesError && devices.length > 0 && (
              <div className="device-list">
                {devices.map((device) => (
                  <article className="device-card" key={device.device_id}>
                    <div className="device-card-heading">
                      <div>
                        <strong>{device.display_name}</strong>
                        <span>{device.platform} · {device.device_id}</span>
                      </div>
                      <span className={`device-status ${device.online ? "online" : "offline"}`}>
                        {device.online ? "Online" : "Offline"}
                      </span>
                    </div>
                    <div className="device-capabilities">
                      {device.capabilities.map((capability) => (
                        <span key={capability}>{capability}</span>
                      ))}
                    </div>
                  </article>
                ))}
              </div>
            )}
          </section>
        </main>
      ) : (
        <main className="panel-page">
          <section className="panel">
            <div className="panel-kicker">
              {page.toUpperCase()}
            </div>

            <h1>
              {page === "memory" && "Memory"}
              {page === "settings" && "Settings"}
            </h1>

            <p>
              {page === "memory" &&
                "Sohbet ge\u00e7mi\u015fi de\u011fil; EPIS'in kal\u0131c\u0131 memory ve identity katman\u0131 burada g\u00f6r\u00fcnecek."}

              {page === "settings" &&
                "Model, privacy, server ve EPIS davran\u0131\u015f ayarlar\u0131 burada olacak."}
            </p>

            <div className="not-connected-yet">
              {"Backend ba\u011flant\u0131s\u0131 sonraki a\u015famada"}
            </div>
          </section>
        </main>
      )}
    </div>
  );
}

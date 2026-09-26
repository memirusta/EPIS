import { ChangeEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import "./App.css";

type Page = "chat" | "usage" | "devices" | "memory" | "settings";

type ToolResult = Record<string, unknown>;

type AttachmentSummary = {
  name: string;
  mimeType: string;
  sizeBytes: number;
};

type PendingAttachment = AttachmentSummary & {
  dataBase64: string;
};

type Message = {
  id: number;
  role: "user" | "assistant";
  text: string;
  toolResults?: ToolResult[];
  messageId?: string;
  requestId?: string;
  seq?: number;
  createdAt?: string;
  dayId?: string;
  attachments?: AttachmentSummary[];
};

type ConnectionState = "connecting" | "online" | "offline";
type AgentConnectionState = "connecting" | "online" | "offline";

type NcTraceMetric = string | number | boolean;

type NcTraceEvent = {
  runId: string;
  dayId: string;
  seq: number;
  event: string;
  stage?: string;
  status?: string;
  title?: string;
  detail?: string;
  metrics: Record<string, NcTraceMetric>;
  time?: string;
};

type NcTraceRun = {
  runId: string;
  dayId: string;
  status: string;
  events: NcTraceEvent[];
  updatedAt?: string;
};

type ApprovalRequest = {
  id: string;
  message: string;
  requestId: string;
  clientId?: string;
  originDeviceId?: string;
  tool?: string;
  capability?: string;
  risk?: string;
};

type ServerConfig = {
  url: string;
  token: string;
};

type ExternalReplyEvent = {
  provider: "whatsapp";
  outreachId: string;
  content: string;
  receivedAt: string;
  contactName?: string;
};

type StartupState = {
  supported: boolean;
  enabled: boolean;
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

const CHAT_STORAGE_KEY = "epis.desktop.chat.v3";
const LEGACY_CHAT_STORAGE_KEYS = ["epis.desktop.chat.v2", "epis.desktop.chat.v1"];
const MAX_PERSISTED_MESSAGES = 80;
const MAX_PERSISTED_TEXT_CHARS = 12000;
const MAX_CHAT_ATTACHMENTS = 3;
const MAX_ATTACHMENT_BYTES = 5_000_000;
const MAX_TEXT_ATTACHMENT_BYTES = 512_000;

const TEXT_ATTACHMENT_EXTENSIONS = new Set([
  ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".dart",
  ".kt", ".kts", ".java", ".rs", ".go", ".c", ".h", ".cpp",
  ".hpp", ".cs", ".json", ".yaml", ".yml", ".toml", ".xml",
  ".html", ".css", ".scss", ".sql", ".sh", ".ps1", ".bat",
  ".csv", ".log", ".ini", ".cfg",
]);


let messageId = 0;

function nextMessageId() {
  messageId += 1;
  return messageId;
}

let protocolIdCounter = 0;

function nextProtocolId(prefix: string): string {
  protocolIdCounter += 1;
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}:${random}:${protocolIdCounter}`;
}

function asApprovalRequest(value: unknown): ApprovalRequest | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }

  const item = value as Record<string, unknown>;

  if (
    typeof item.id !== "string" ||
    typeof item.message !== "string" ||
    typeof item.request_id !== "string"
  ) {
    return null;
  }

  return {
    id: item.id,
    message: item.message,
    requestId: item.request_id,
    clientId: typeof item.client_id === "string" ? item.client_id : undefined,
    originDeviceId:
      typeof item.origin_device_id === "string"
        ? item.origin_device_id
        : undefined,
    tool: typeof item.tool === "string" ? item.tool : undefined,
    capability:
      typeof item.capability === "string" ? item.capability : undefined,
    risk: typeof item.risk === "string" ? item.risk : undefined,
  };
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

function attachmentExtension(name: string): string {
  const index = name.lastIndexOf(".");
  return index >= 0
    ? name.slice(index).toLowerCase()
    : "";
}

function attachmentMime(file: File): string {
  const declared = file.type
    .trim()
    .toLowerCase();

  if (declared) {
    return declared;
  }

  const extension =
    attachmentExtension(file.name);

  if (extension === ".png") {
    return "image/png";
  }

  if (
    extension === ".jpg" ||
    extension === ".jpeg"
  ) {
    return "image/jpeg";
  }

  if (extension === ".webp") {
    return "image/webp";
  }

  if (extension === ".gif") {
    return "image/gif";
  }

  return TEXT_ATTACHMENT_EXTENSIONS.has(
    extension,
  )
    ? "text/plain"
    : "application/octet-stream";
}

function isTextAttachment(
  file: File,
  mimeType: string,
): boolean {
  if (
    mimeType.startsWith("text/")
  ) {
    return true;
  }

  if (
    [
      "application/json",
      "application/xml",
      "application/javascript",
      "application/x-javascript",
      "application/yaml",
      "application/x-yaml",
    ].includes(mimeType)
  ) {
    return true;
  }

  return TEXT_ATTACHMENT_EXTENSIONS.has(
    attachmentExtension(file.name),
  );
}

function fileToBase64(
  file: File,
): Promise<string> {
  return new Promise(
    (resolve, reject) => {
      const reader =
        new FileReader();

      reader.onerror = () =>
        reject(
          new Error(
            "file_read_failed",
          ),
        );

      reader.onload = () => {
        if (
          typeof reader.result !==
          "string"
        ) {
          reject(
            new Error(
              "file_read_failed",
            ),
          );
          return;
        }

        const comma =
          reader.result.indexOf(",");

        if (comma < 0) {
          reject(
            new Error(
              "file_read_failed",
            ),
          );
          return;
        }

        resolve(
          reader.result.slice(
            comma + 1,
          ),
        );
      };

      reader.readAsDataURL(
        file,
      );
    },
  );
}

function attachmentSizeLabel(
  bytes: number,
): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }

  if (
    bytes <
    1024 * 1024
  ) {
    return `${Math.round(
      bytes / 1024,
    )} KB`;
  }

  return `${(
    bytes /
    (1024 * 1024)
  ).toFixed(1)} MB`;
}

function asAttachmentSummary(
  value: unknown,
): AttachmentSummary | null {
  const item = asObject(value);

  if (item === null) {
    return null;
  }

  if (
    typeof item.name !== "string" ||
    typeof item.mime_type !== "string" ||
    typeof item.size_bytes !== "number"
  ) {
    return null;
  }

  return {
    name: item.name,
    mimeType: item.mime_type,
    sizeBytes: item.size_bytes,
  };
}

function localDayId(): string {
  const date = new Date();
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function asCanonicalMessage(value: unknown): Message | null {
  const item = asObject(value);
  if (item === null) return null;
  if (item.role !== "user" && item.role !== "assistant") return null;
  if (typeof item.text !== "string" || !item.text.trim()) return null;

  return {
    id: nextMessageId(),
    role: item.role,
    text: item.text.slice(0, MAX_PERSISTED_TEXT_CHARS),
    messageId: typeof item.message_id === "string" ? item.message_id : undefined,
    requestId: typeof item.request_id === "string" ? item.request_id : undefined,
    seq: typeof item.seq === "number" ? item.seq : undefined,
    createdAt:
      typeof item.created_at === "string" ? item.created_at : undefined,
    dayId: typeof item.day_id === "string" ? item.day_id : undefined,
    attachments: Array.isArray(item.attachments)
      ? item.attachments
          .map(asAttachmentSummary)
          .filter(
            (
              attachment,
            ): attachment is AttachmentSummary =>
              attachment !== null,
          )
      : undefined,
  };
}

function asExternalReplyEvent(value: unknown): ExternalReplyEvent | null {
  const item = asObject(value);
  if (
    item === null ||
    item.provider !== "whatsapp" ||
    typeof item.outreach_id !== "string" ||
    !item.outreach_id.trim() ||
    typeof item.content !== "string" ||
    !item.content.trim() ||
    typeof item.received_at !== "string" ||
    !item.received_at.trim()
  ) {
    return null;
  }

  return {
    provider: "whatsapp",
    outreachId: item.outreach_id,
    content: item.content.slice(0, MAX_PERSISTED_TEXT_CHARS),
    receivedAt: item.received_at,
    contactName:
      typeof item.contact_name === "string" && item.contact_name.trim()
        ? item.contact_name.trim().slice(0, 256)
        : undefined,
  };
}

function loadStoredMessages(): Message[] {
  try {
    const currentDay = localDayId();
    const currentRaw = window.localStorage.getItem(CHAT_STORAGE_KEY);
    if (currentRaw) {
      const envelope: unknown = JSON.parse(currentRaw);
      const object = asObject(envelope);
      if (
        object !== null &&
        object.day_id === currentDay &&
        Array.isArray(object.messages)
      ) {
        return object.messages
          .map(asCanonicalMessage)
          .filter((item): item is Message => item !== null)
          .slice(-MAX_PERSISTED_MESSAGES);
      }
    }

    // One-time compatibility import for the pre-canonical desktop cache.
    for (const key of LEGACY_CHAT_STORAGE_KEYS) {
      const raw = window.localStorage.getItem(key);
      if (!raw) continue;
      const parsed: unknown = JSON.parse(raw);
      if (!Array.isArray(parsed)) continue;
      return parsed
        .map(asCanonicalMessage)
        .filter((item): item is Message => item !== null)
        .slice(-MAX_PERSISTED_MESSAGES);
    }
    return [];
  } catch {
    return [];
  }
}

function sameCanonicalMessage(left: Message, right: Message): boolean {
  if (left.messageId && right.messageId) {
    return left.messageId === right.messageId;
  }
  if (left.requestId && right.requestId && left.role === right.role) {
    return left.requestId === right.requestId;
  }
  return false;
}

function mergeCanonicalMessage(current: Message[], incoming: Message): Message[] {
  const index = current.findIndex((item) => sameCanonicalMessage(item, incoming));
  if (index < 0) return [...current, incoming];
  const next = [...current];
  next[index] = { ...current[index], ...incoming, id: current[index].id };
  return next;
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

function asNcTraceEvent(value: unknown): NcTraceEvent | null {
  const item = asObject(value);

  if (item === null) return null;

  if (
    typeof item.run_id !== "string" ||
    typeof item.day_id !== "string" ||
    typeof item.seq !== "number" ||
    typeof item.event !== "string"
  ) {
    return null;
  }

  const metrics: Record<string, NcTraceMetric> = {};
  const rawMetrics = asObject(item.metrics);

  if (rawMetrics !== null) {
    for (const [key, raw] of Object.entries(rawMetrics)) {
      if (
        typeof raw === "string" ||
        typeof raw === "number" ||
        typeof raw === "boolean"
      ) {
        metrics[key] = raw;
      }
    }
  }

  return {
    runId: item.run_id,
    dayId: item.day_id,
    seq: item.seq,
    event: item.event,
    stage:
      typeof item.stage === "string"
        ? item.stage
        : undefined,
    status:
      typeof item.status === "string"
        ? item.status
        : undefined,
    title:
      typeof item.title === "string"
        ? item.title
        : undefined,
    detail:
      typeof item.detail === "string"
        ? item.detail
        : undefined,
    metrics,
    time:
      typeof item.time === "string"
        ? item.time
        : undefined,
  };
}

function traceRunStatus(
  event: NcTraceEvent,
  current: string,
): string {
  if (event.event === "run.failed") {
    return "failed";
  }

  if (event.event === "run.completed") {
    return event.status ?? "success";
  }

  if (event.event === "run.started") {
    return "running";
  }

  return current;
}

function mergeNcTraceEvent(
  current: NcTraceRun | null,
  event: NcTraceEvent,
): NcTraceRun {
  const base: NcTraceRun =
    current !== null &&
    current.runId === event.runId
      ? current
      : {
          runId: event.runId,
          dayId: event.dayId,
          status: "running",
          events: [],
        };

  const bySeq = new Map(
    base.events.map((item) => [
      item.seq,
      item,
    ]),
  );

  bySeq.set(event.seq, event);

  return {
    ...base,
    dayId: event.dayId,
    status: traceRunStatus(
      event,
      base.status,
    ),
    events: [...bySeq.values()].sort(
      (left, right) =>
        left.seq - right.seq,
    ),
    updatedAt:
      event.time ?? base.updatedAt,
  };
}

function asNcTraceSnapshot(
  value: unknown,
): NcTraceRun | null {
  const item = asObject(value);

  if (
    item === null ||
    typeof item.run_id !== "string" ||
    typeof item.day_id !== "string" ||
    !Array.isArray(item.events)
  ) {
    return null;
  }

  const events = item.events
    .map(asNcTraceEvent)
    .filter(
      (event): event is NcTraceEvent =>
        event !== null,
    )
    .sort(
      (left, right) =>
        left.seq - right.seq,
    );

  return {
    runId: item.run_id,
    dayId: item.day_id,
    status:
      typeof item.status === "string"
        ? item.status
        : "running",
    events,
    updatedAt:
      typeof item.updated_at === "string"
        ? item.updated_at
        : undefined,
  };
}

function traceStatusLabel(
  status: string,
): string {
  switch (status) {
    case "running":
      return "Running";

    case "success":
      return "Complete";

    case "partial":
      return "Partial";

    case "failed":
      return "Failed";

    case "committed":
      return "Committed";

    case "acked":
      return "Acked";

    case "pending":
      return "Pending";

    case "ok":
      return "OK";

    case "skip":
      return "Skipped";

    case "warn":
      return "Warning";

    case "error":
      return "Error";

    default:
      return status || "Event";
  }
}

function traceMetricLabel(
  key: string,
): string {
  return key.replace(/_/g, " ");
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
  const reconnectDelayRef = useRef(1000);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef =
    useRef<HTMLInputElement | null>(null);
  const clientIdRef = useRef<string>(nextProtocolId("desktop"));
  const messagesRef = useRef<Message[]>([]);
  const externalReplyOutreachIdsRef = useRef<Set<string>>(new Set());
  const importAttemptedRef = useRef(false);
  const activeDayRef = useRef<string>(localDayId());

  const [page, setPage] = useState<Page>("chat");
  const [connection, setConnection] =
    useState<ConnectionState>("connecting");
  const [agentConnection, setAgentConnection] =
    useState<AgentConnectionState>("connecting");

  const [serverVersion, setServerVersion] = useState("");
  const [serverConfig, setServerConfig] = useState<ServerConfig | null>(null);
  const [messages, setMessages] =
    useState<Message[]>(loadStoredMessages);
  const [text, setText] = useState("");

  const [
    pendingAttachments,
    setPendingAttachments,
  ] = useState<PendingAttachment[]>([]);

  const [
    readingAttachments,
    setReadingAttachments,
  ] = useState(false);
  const [inFlightRequests, setInFlightRequests] =
    useState<Set<string>>(() => new Set());
  const [approvals, setApprovals] =
    useState<ApprovalRequest[]>([]);
  const [approvalSubmitting, setApprovalSubmitting] =
    useState<Set<string>>(() => new Set());
  const waiting = inFlightRequests.size > 0;
  const [usage, setUsage] = useState<UsageSnapshot | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageError, setUsageError] = useState<string | null>(null);
  const [devices, setDevices] = useState<DeviceSnapshot[]>([]);
  const [devicesLoading, setDevicesLoading] = useState(false);
  const [devicesError, setDevicesError] = useState<string | null>(null);
  const [startupState, setStartupState] = useState<StartupState | null>(null);
  const [startupSaving, setStartupSaving] = useState(false);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [ncTrace, setNcTrace] = useState<NcTraceRun | null>(null);

  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    messagesRef.current = messages;
    try {
      const persisted = messages
        .slice(-MAX_PERSISTED_MESSAGES)
        .map(({ role, text, messageId, requestId, seq, createdAt, dayId }) => ({
          role,
          text: text.slice(0, MAX_PERSISTED_TEXT_CHARS),
          message_id: messageId,
          request_id: requestId,
          seq,
          created_at: createdAt,
          day_id: dayId,
        }));
      window.localStorage.setItem(
        CHAT_STORAGE_KEY,
        JSON.stringify({ day_id: activeDayRef.current, messages: persisted }),
      );
      for (const key of LEGACY_CHAT_STORAGE_KEYS) {
        window.localStorage.removeItem(key);
      }
    } catch {
      // UI persistence failure must not break chat.
    }
  }, [messages]);

  useEffect(() => {
    invoke<ServerConfig>("server_config")
      .then((config) => {
        setServerConfig(config);
        setError(null);
      })
      .catch((reason) => {
        setServerConfig(null);
        setConnection("offline");
        setAgentConnection("offline");
        setError(
          `EPIS cloud configuration unavailable: ${String(reason)}`,
        );
      });
  }, []);

  function markInFlight(requestId: string) {
    if (!requestId) return;
    setInFlightRequests((current) => {
      const next = new Set(current);
      next.add(requestId);
      return next;
    });
  }

  function finishInFlight(requestId: unknown) {
    if (typeof requestId !== "string" || !requestId) return;
    setInFlightRequests((current) => {
      if (!current.has(requestId)) return current;
      const next = new Set(current);
      next.delete(requestId);
      return next;
    });
  }

  function queueApproval(approval: ApprovalRequest) {
    setApprovals((current) => {
      const withoutOld = current.filter((item) => item.id !== approval.id);
      return [...withoutOld, approval];
    });
  }

  function removeApproval(approvalId: unknown) {
    if (typeof approvalId !== "string") return;
    setApprovals((current) =>
      current.filter((item) => item.id !== approvalId),
    );
    setApprovalSubmitting((current) => {
      if (!current.has(approvalId)) return current;
      const next = new Set(current);
      next.delete(approvalId);
      return next;
    });
  }

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

        setConnection("connecting");
        setAgentConnection("connecting");
        setError(null);
        ws.send(
          JSON.stringify({
            type: "client.hello",
            version: 2,
            client_id: clientIdRef.current,
            client_type: "desktop",
          }),
        );
      };

      ws.onmessage = (event) => {
        if (disposed || socketRef.current !== ws) {
          return;
        }

        try {
          const data = JSON.parse(event.data);

          if (data.type === "connected") {
            setServerVersion(data.version ?? "");
            if (typeof data.day_id === "string") {
              activeDayRef.current = data.day_id;
            }
            return;
          }

          if (data.type === "client.ready") {
            reconnectDelayRef.current = 1000;
            setConnection("online");
            setError(null);
            ws.send(JSON.stringify({ type: "devices.get" }));
            ws.send(JSON.stringify({
              type: "conversation.sync",
              request_id: nextProtocolId("sync"),
            }));
            return;
          }

          if (data.type === "nc.trace.snapshot") {
            const snapshot =
              asNcTraceSnapshot(data);

            if (snapshot !== null) {
              setNcTrace(snapshot);
            }

            return;
          }

          if (data.type === "nc.trace") {
            const traceEvent =
              asNcTraceEvent(data);

            if (traceEvent !== null) {
              setNcTrace((current) =>
                mergeNcTraceEvent(
                  current,
                  traceEvent,
                ),
              );
            }

            return;
          }

          if (data.type === "chat.accepted") {
            const canonical = asCanonicalMessage(data.message);
            if (canonical !== null) {
              if (canonical.dayId && canonical.dayId !== activeDayRef.current) {
                activeDayRef.current = canonical.dayId;
                setMessages([canonical]);
              } else {
                setMessages((current) =>
                  current.map((item) =>
                    item.requestId === canonical.requestId && item.role === "user"
                      ? { ...item, ...canonical, id: item.id }
                      : item,
                  ),
                );
              }
            }
            return;
          }

          if (data.type === "conversation.snapshot") {
            const raw = Array.isArray(data.messages) ? data.messages : [];
            const snapshot = raw
              .map(asCanonicalMessage)
              .filter((item: Message | null): item is Message => item !== null);
            const snapshotDay =
              typeof data.day_id === "string" ? data.day_id : activeDayRef.current;
            const previousDay = activeDayRef.current;
            activeDayRef.current = snapshotDay;

            if (
              snapshot.length === 0 &&
              previousDay === snapshotDay &&
              messagesRef.current.length > 0 &&
              !importAttemptedRef.current
            ) {
              importAttemptedRef.current = true;
              ws.send(JSON.stringify({
                type: "conversation.import",
                request_id: nextProtocolId("import"),
                messages: messagesRef.current.map(({ role, text }) => ({ role, text })),
              }));
              return;
            }

            setMessages(snapshot);
            return;
          }

          if (data.type === "conversation.imported") {
            return;
          }

          if (data.type === "conversation.live_message") {
            const incoming = asCanonicalMessage(data);
            if (incoming !== null) {
              if (incoming.dayId && incoming.dayId !== activeDayRef.current) {
                activeDayRef.current = incoming.dayId;
                setMessages([incoming]);
              } else {
                setMessages((current) => mergeCanonicalMessage(current, incoming));
              }
            }
            return;
          }

          if (data.type === "assistant.message") {
            const rawResults: unknown[] = Array.isArray(data.tool_results)
              ? data.tool_results
              : [];

            const parsedResults = rawResults
              .map(asObject)
              .filter((item): item is ToolResult => item !== null);
            const messageText = String(data.text ?? "").trim();

            if (messageText || parsedResults.length > 0) {
              const incoming: Message = {
                id: nextMessageId(),
                role: "assistant",
                text: messageText,
                toolResults: parsedResults,
                messageId: typeof data.message_id === "string" ? data.message_id : undefined,
                requestId: typeof data.request_id === "string" ? data.request_id : undefined,
                seq: typeof data.seq === "number" ? data.seq : undefined,
                createdAt:
                  typeof data.created_at === "string" ? data.created_at : undefined,
                dayId: typeof data.day_id === "string" ? data.day_id : undefined,
              };
              if (incoming.dayId && incoming.dayId !== activeDayRef.current) {
                activeDayRef.current = incoming.dayId;
                setMessages([incoming]);
              } else {
                setMessages((current) => mergeCanonicalMessage(current, incoming));
              }
            }

            if (Boolean(data.confirmation_required)) {
              const approval = asApprovalRequest(data.approval);
              if (approval === null) {
                setError("Onay isteği okunamadı.");
              } else {
                queueApproval(approval);
              }
            }

            finishInFlight(data.request_id);
            return;
          }

          if (data.type === "proactive.message") {
            const textValue = String(data.text ?? "").trim();
            if (textValue) {
              const incoming: Message = {
                id: nextMessageId(),
                role: "assistant",
                text: textValue,
                messageId: typeof data.message_id === "string" ? data.message_id : undefined,
                requestId: typeof data.request_id === "string" ? data.request_id : undefined,
                seq: typeof data.seq === "number" ? data.seq : undefined,
                createdAt:
                  typeof data.created_at === "string" ? data.created_at : undefined,
                dayId: typeof data.day_id === "string" ? data.day_id : undefined,
              };
              if (incoming.dayId && incoming.dayId !== activeDayRef.current) {
                activeDayRef.current = incoming.dayId;
                setMessages([incoming]);
              } else {
                setMessages((current) => mergeCanonicalMessage(current, incoming));
              }
            }
            return;
          }

          if (data.type === "external.reply") {
            const reply = asExternalReplyEvent(data);
            if (reply === null || externalReplyOutreachIdsRef.current.has(reply.outreachId)) {
              return;
            }

            externalReplyOutreachIdsRef.current.add(reply.outreachId);
            const sender = reply.contactName ?? "Bir kişi";
            const incoming: Message = {
              id: nextMessageId(),
              role: "assistant",
              text: `${sender} WhatsApp'tan cevap verdi:\n${reply.content}`,
              messageId: `external.reply:${reply.outreachId}`,
              createdAt: reply.receivedAt,
            };
            setMessages((current) => mergeCanonicalMessage(current, incoming));
            return;
          }

          if (data.type === "approval.accepted") {
            removeApproval(data.approval_id);
            if (typeof data.request_id === "string") {
              markInFlight(data.request_id);
            }
            return;
          }

          if (data.type === "conversation.accepted") {
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
              const windowsOnline = snapshot.some(
                (device) => device.online && device.platform.toLowerCase() === "windows",
              );
              setAgentConnection(windowsOnline ? "online" : "offline");
            }
            setDevicesLoading(false);
            return;
          }

          if (data.type === "conversation.reset") {
            finishInFlight(data.request_id);
            if (!Boolean(data.preserved_daily_transcript)) {
              setMessages([]);
              setInFlightRequests(new Set());
            }
            setApprovals([]);
            setApprovalSubmitting(new Set());
            setError(null);
            return;
          }

          if (data.type === "error") {
            finishInFlight(data.request_id);

            if (typeof data.approval_id === "string") {
              setApprovalSubmitting((current) => {
                if (!current.has(data.approval_id)) return current;
                const next = new Set(current);
                next.delete(data.approval_id);
                return next;
              });
            }

            setError(
              data.detail ??
                data.error ??
                "Bilinmeyen server hatas\u0131.",
            );

            return;
          }
        } catch {
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
        setAgentConnection("offline");

        const delay = reconnectDelayRef.current;
        reconnectDelayRef.current = Math.min(delay * 2, 30000);
        reconnectTimerRef.current = window.setTimeout(
          connect,
          delay + Math.floor(Math.random() * Math.min(delay, 1000)),
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
    if (connection !== "online") return;
    const timer = window.setInterval(() => {
      const ws = socketRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "devices.get" }));
      }
    }, 30000);
    return () => window.clearInterval(timer);
  }, [connection]);

  useEffect(() => {
    if (page !== "settings") return;
    setSettingsError(null);
    invoke<StartupState>("startup_state")
      .then(setStartupState)
      .catch(() => setSettingsError("Windows başlangıç ayarı okunamadı."));
  }, [page]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "end",
    });
  }, [messages, waiting, approvals.length]);

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

  async function handleAttachmentSelection(
    event: ChangeEvent<HTMLInputElement>,
  ) {
    const selected =
      event.target.files
        ? Array.from(
            event.target.files,
          )
        : [];

    event.target.value = "";

    if (
      selected.length === 0
    ) {
      return;
    }

    const remaining =
      MAX_CHAT_ATTACHMENTS -
      pendingAttachments.length;

    if (remaining <= 0) {
      setError(
        "Bir mesaja en fazla 3 dosya ekleyebilirsin.",
      );
      return;
    }

    setReadingAttachments(true);
    setError(null);

    try {
      const accepted:
        PendingAttachment[] = [];

      const errors: string[] = [];

      for (
        const file of selected.slice(
          0,
          remaining,
        )
      ) {
        const mimeType =
          attachmentMime(file);

        const textFile =
          isTextAttachment(
            file,
            mimeType,
          );

        const imageFile =
          mimeType.startsWith(
            "image/",
          );

        if (
          !textFile &&
          !imageFile
        ) {
          errors.push(
            `${file.name}: bu dosya türü henüz desteklenmiyor.`,
          );
          continue;
        }

        if (
          file.size >
          MAX_ATTACHMENT_BYTES
        ) {
          errors.push(
            `${file.name}: 5 MB sınırını aşıyor.`,
          );
          continue;
        }

        if (
          textFile &&
          file.size >
            MAX_TEXT_ATTACHMENT_BYTES
        ) {
          errors.push(
            `${file.name}: metin dosyaları en fazla 512 KB olabilir.`,
          );
          continue;
        }

        try {
          accepted.push({
            name:
              file.name.slice(
                0,
                180,
              ) ||
              "attachment",

            mimeType,

            sizeBytes:
              file.size,

            dataBase64:
              await fileToBase64(
                file,
              ),
          });
        } catch {
          errors.push(
            `${file.name}: dosya okunamadı.`,
          );
        }
      }

      if (
        selected.length >
        remaining
      ) {
        errors.push(
          "Bir mesaja en fazla 3 dosya ekleyebilirsin.",
        );
      }

      if (
        accepted.length > 0
      ) {
        setPendingAttachments(
          (current) =>
            [
              ...current,
              ...accepted,
            ].slice(
              0,
              MAX_CHAT_ATTACHMENTS,
            ),
        );
      }

      setError(
        errors[0] ?? null,
      );
    } finally {
      setReadingAttachments(
        false,
      );
    }
  }

  function removeAttachment(
    index: number,
  ) {
    setPendingAttachments(
      (current) =>
        current.filter(
          (
            _,
            itemIndex,
          ) =>
            itemIndex !== index,
        ),
    );
  }

  function send() {
    const value = text.trim();

    const attachments =
      pendingAttachments;

    if (
      !value &&
      attachments.length === 0
    ) {
      return;
    }

    const requestId =
      nextProtocolId("chat");

    const displayText =
      value ||
      "Ekli dosyayı incele.";

    if (
      !sendPacket({
        type: "chat.send",
        request_id: requestId,

        origin_device_id:
          `desktop:${clientIdRef.current}`,

        text: value,

        ...(attachments.length > 0
          ? {
              attachments:
                attachments.map(
                  (attachment) => ({
                    name:
                      attachment.name,

                    mime_type:
                      attachment.mimeType,

                    size_bytes:
                      attachment.sizeBytes,

                    data_base64:
                      attachment.dataBase64,
                  }),
                ),
            }
          : {}),
      })
    ) {
      return;
    }

    markInFlight(requestId);

    setMessages(
      (current) => [
        ...current,
        {
          id:
            nextMessageId(),

          role:
            "user",

          text:
            displayText,

          requestId,

          attachments:
            attachments.map(
              ({
                name,
                mimeType,
                sizeBytes,
              }) => ({
                name,
                mimeType,
                sizeBytes,
              }),
            ),
        },
      ],
    );

    setText("");

    setPendingAttachments(
      [],
    );

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

  function approve(approval: ApprovalRequest) {
    if (approvalSubmitting.has(approval.id)) {
      return;
    }

    const operationId = nextProtocolId("approval");

    if (
      !sendPacket({
        type: "approval.confirm",
        approval_id: approval.id,
        operation_id: operationId,
      })
    ) {
      return;
    }

    setApprovalSubmitting((current) => {
      const next = new Set(current);
      next.add(approval.id);
      return next;
    });
    setError(null);
  }

  function reject(approval: ApprovalRequest) {
    if (approvalSubmitting.has(approval.id)) {
      return;
    }

    const operationId = nextProtocolId("approval");

    if (
      !sendPacket({
        type: "approval.reject",
        approval_id: approval.id,
        operation_id: operationId,
      })
    ) {
      return;
    }

    setApprovalSubmitting((current) => {
      const next = new Set(current);
      next.add(approval.id);
      return next;
    });
    setError(null);
  }

  async function toggleStartup(enabled: boolean) {
    if (startupSaving) return;
    setStartupSaving(true);
    setSettingsError(null);
    try {
      const state = await invoke<StartupState>("set_startup_enabled", { enabled });
      setStartupState(state);
    } catch {
      setSettingsError("Windows başlangıç ayarı değiştirilemedi.");
    } finally {
      setStartupSaving(false);
    }
  }

  function clearContext() {
    const requestId = nextProtocolId("conversation");

    if (
      !sendPacket({
        type: "conversation.new",
        request_id: requestId,
      })
    ) {
      return;
    }

    markInFlight(requestId);
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
            Nightly
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

          <div className={`agent-pill ${agentConnection}`}>
            <span className="dot" />
            {agentConnection === "online"
              ? "PC Agent"
              : agentConnection === "connecting"
                ? "Agent bağlanıyor"
                : "Agent offline"}
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

                    {message.attachments &&
                      message.attachments.length > 0 && (
                        <div className="message-attachments">
                          {message.attachments.map(
                            (
                              attachment,
                              index,
                            ) => (
                              <span
                                key={`${attachment.name}:${index}`}
                              >
                                <strong>
                                  {attachment.name}
                                </strong>

                                <small>
                                  {attachmentSizeLabel(
                                    attachment.sizeBytes,
                                  )}
                                </small>
                              </span>
                            ),
                          )}
                        </div>
                      )}

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
                        {inFlightRequests.size > 1
                          ? `EPIS ${inFlightRequests.size} isteği çalıştırıyor`
                          : "EPIS çalışıyor"}
                      </span>
                    </div>
                  </article>
                )}

                {approvals.map((approval) => {
                  const submitting = approvalSubmitting.has(approval.id);

                  return (
                    <section
                      className="permission-card"
                      key={approval.id}
                    >
                      <div className="permission-icon">
                        !
                      </div>

                      <div className="permission-copy">
                        <strong>Onay gerekiyor</strong>

                        <span>{approval.message}</span>
                      </div>

                      <div className="permission-actions">
                        <button
                          className="secondary"
                          disabled={submitting}
                          onClick={() => reject(approval)}
                        >
                          Hayır
                        </button>

                        <button
                          className="primary"
                          disabled={submitting}
                          onClick={() => approve(approval)}
                        >
                          {submitting ? "Gönderiliyor..." : "Evet"}
                        </button>
                      </div>
                    </section>
                  );
                })}

                <div ref={bottomRef} />
              </div>
            )}
          </main>

          <footer className="composer-zone">
            <div className="chat-actions">
              <button
                onClick={clearContext}
                disabled={connection !== "online"}
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

            {pendingAttachments.length > 0 && (
              <div className="attachment-strip">
                {pendingAttachments.map(
                  (
                    attachment,
                    index,
                  ) => (
                    <div
                      className="attachment-chip"
                      key={`${attachment.name}:${index}`}
                    >
                      <span className="attachment-kind">
                        {attachment.mimeType.startsWith(
                          "image/",
                        )
                          ? "IMG"
                          : "FILE"}
                      </span>

                      <span className="attachment-copy">
                        <strong
                          title={attachment.name}
                        >
                          {attachment.name}
                        </strong>

                        <small>
                          {attachmentSizeLabel(
                            attachment.sizeBytes,
                          )}
                        </small>
                      </span>

                      <button
                        type="button"
                        aria-label={
                          `${attachment.name} ekini kaldır`
                        }
                        onClick={() =>
                          removeAttachment(
                            index,
                          )
                        }
                      >
                        ×
                      </button>
                    </div>
                  ),
                )}
              </div>
            )}

            <input
              ref={fileInputRef}
              className="attachment-input"
              type="file"
              multiple
              accept="image/*,.txt,.md,.py,.js,.ts,.tsx,.jsx,.dart,.kt,.kts,.java,.rs,.go,.c,.h,.cpp,.hpp,.cs,.json,.yaml,.yml,.toml,.xml,.html,.css,.scss,.sql,.sh,.ps1,.bat,.csv,.log,.ini,.cfg"
              onChange={
                handleAttachmentSelection
              }
            />

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
                disabled={
                  connection !==
                    "online" ||
                  readingAttachments ||
                  pendingAttachments.length >=
                    MAX_CHAT_ATTACHMENTS
                }
                title="Dosya ekle"
                onClick={() =>
                  fileInputRef.current?.click()
                }
              >
                {readingAttachments
                  ? "…"
                  : "+"}
              </button>

              <textarea
                value={text}
                disabled={connection !== "online"}
                placeholder={
                  connection !== "online"
                    ? "Server ba\u011flant\u0131s\u0131 bekleniyor..."
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
                  (!text.trim() &&
                    pendingAttachments.length ===
                      0) ||
                  readingAttachments ||
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
      ) : page === "settings" ? (
        <main className="panel-page">
          <section className="panel">
            <div className="panel-kicker">SETTINGS · DESKTOP</div>
            <h1>Settings</h1>
            <p>Desktop agent ve Windows başlangıç davranışı.</p>

            <div className="settings-list">
              <article className="settings-card">
                <div>
                  <strong>PC Agent</strong>
                  <span>EPIS Desktop açıldığında Windows Agent sessizce başlar ve Cloud Core bağlantısını otomatik yeniden kurar.</span>
                </div>
                <span className={`settings-status ${agentConnection}`}>
                  {agentConnection === "online" ? "Connected" : agentConnection === "connecting" ? "Reconnecting" : "Offline"}
                </span>
              </article>

              <article className="settings-card">
                <div>
                  <strong>Start EPIS Agent with Windows</strong>
                  <span>Windows oturumu açıldığında EPIS arka planda başlar. Pencereyi kapatmak uygulamayı tray'e küçültür.</span>
                </div>
                <label className="switch">
                  <input
                    type="checkbox"
                    checked={startupState?.enabled ?? false}
                    disabled={startupSaving || startupState === null || !startupState.supported}
                    onChange={(event) => void toggleStartup(event.target.checked)}
                  />
                  <span className="switch-track" />
                </label>
              </article>
            </div>

            {settingsError && <div className="usage-state error-state">{settingsError}</div>}
            <div className="settings-note">X düğmesi yalnızca pencereyi gizler. EPIS Agent tray'de çalışmaya devam eder. Tamamen kapatmak için tray menüsündeki “Quit EPIS” seçeneğini kullan.</div>
          </section>
        </main>
      ) : (
        <main className="panel-page">
          <section className="panel nightly-panel">
            <div className="panel-kicker">
              MEMORY · NIGHTLY · LIVE
            </div>

            <div className="nightly-heading">
              <div>
                <h1>Nightly Activity</h1>
                <p>
                  NC'nin gözlemlenebilir çalışma izi.
                  Raw transcript, prompt ve chain-of-thought
                  bu akışa çıkmaz.
                </p>
              </div>

              {ncTrace !== null && (
                <span
                  className={`nightly-run-status ${ncTrace.status}`}
                >
                  {traceStatusLabel(
                    ncTrace.status,
                  )}
                </span>
              )}
            </div>

            {ncTrace === null ? (
              <div className="not-connected-yet">
                Henüz bir Nightly Recalculation izi alınmadı.
                Sonraki NC başladığında bu ekran canlı güncellenecek.
              </div>
            ) : (
              <>
                <div className="nightly-run-meta">
                  <div>
                    <span>Run</span>
                    <strong>
                      {ncTrace.runId}
                    </strong>
                  </div>

                  <div>
                    <span>Day</span>
                    <strong>
                      {ncTrace.dayId}
                    </strong>
                  </div>

                  <div>
                    <span>Events</span>
                    <strong>
                      {ncTrace.events.length}
                    </strong>
                  </div>
                </div>

                <div className="nightly-timeline">
                  {ncTrace.events.map(
                    (item) => (
                      <article
                        className={`nightly-event ${item.status ?? ""}`}
                        key={`${item.runId}:${item.seq}`}
                      >
                        <div className="nightly-event-rail">
                          <span className="nightly-event-dot" />
                          <span className="nightly-event-line" />
                        </div>

                        <div className="nightly-event-body">
                          <div className="nightly-event-heading">
                            <div>
                              <span className="nightly-event-stage">
                                {item.stage ??
                                  item.event}
                              </span>

                              <strong>
                                {item.title ??
                                  item.event}
                              </strong>
                            </div>

                            <span
                              className={`nightly-event-status ${item.status ?? ""}`}
                            >
                              {traceStatusLabel(
                                item.status ?? "",
                              )}
                            </span>
                          </div>

                          {item.detail && (
                            <p>
                              {item.detail}
                            </p>
                          )}

                          {Object.keys(
                            item.metrics,
                          ).length > 0 && (
                            <div className="nightly-metrics">
                              {Object.entries(
                                item.metrics,
                              ).map(
                                ([
                                  key,
                                  value,
                                ]) => (
                                  <span
                                    key={key}
                                  >
                                    <small>
                                      {traceMetricLabel(
                                        key,
                                      )}
                                    </small>

                                    <strong>
                                      {String(
                                        value,
                                      )}
                                    </strong>
                                  </span>
                                ),
                              )}
                            </div>
                          )}
                        </div>
                      </article>
                    ),
                  )}
                </div>
              </>
            )}
          </section>
        </main>
      )}
    </div>
  );
}

enum EpisConnectionStatus { connecting, online, offline }

enum ChatRole { user, assistant }

class ChatAttachmentSummary {
  const ChatAttachmentSummary({
    required this.name,
    required this.mimeType,
    required this.sizeBytes,
  });

  final String name;
  final String mimeType;
  final int sizeBytes;

  static ChatAttachmentSummary? tryParse(Object? value) {
    if (value is! Map) return null;
    final map = Map<String, dynamic>.from(value);
    final name = map['name'];
    final mimeType = map['mime_type'];
    final sizeBytes = map['size_bytes'];
    if (name is! String || mimeType is! String || sizeBytes is! num) {
      return null;
    }
    return ChatAttachmentSummary(
      name: name,
      mimeType: mimeType,
      sizeBytes: sizeBytes.toInt(),
    );
  }
}

class ChatAttachment {
  const ChatAttachment({
    required this.name,
    required this.mimeType,
    required this.sizeBytes,
    required this.dataBase64,
  });

  final String name;
  final String mimeType;
  final int sizeBytes;
  final String dataBase64;

  ChatAttachmentSummary get summary => ChatAttachmentSummary(
    name: name,
    mimeType: mimeType,
    sizeBytes: sizeBytes,
  );

  Map<String, dynamic> toWire() => {
    'name': name,
    'mime_type': mimeType,
    'size_bytes': sizeBytes,
    'data_base64': dataBase64,
  };

  static ChatAttachment? tryParseNative(Map<String, dynamic> map) {
    final name = map['name'];
    final mimeType = map['mime_type'];
    final sizeBytes = map['size_bytes'];
    final dataBase64 = map['data_base64'];
    if (name is! String ||
        mimeType is! String ||
        sizeBytes is! num ||
        dataBase64 is! String ||
        name.trim().isEmpty ||
        dataBase64.isEmpty) {
      return null;
    }
    return ChatAttachment(
      name: name.trim(),
      mimeType: mimeType.trim().isEmpty
          ? 'application/octet-stream'
          : mimeType.trim(),
      sizeBytes: sizeBytes.toInt(),
      dataBase64: dataBase64,
    );
  }
}

class ChatMessage {
  const ChatMessage({
    required this.role,
    required this.text,
    this.toolResults = const [],
    this.attachments = const [],
    this.messageId,
    this.requestId,
    this.seq,
    this.createdAt,
    this.dayId,
  });

  final ChatRole role;
  final String text;
  final List<Map<String, dynamic>> toolResults;
  final List<ChatAttachmentSummary> attachments;
  final String? messageId;
  final String? requestId;
  final int? seq;
  final String? createdAt;
  final String? dayId;

  ChatMessage copyWith({
    ChatRole? role,
    String? text,
    List<Map<String, dynamic>>? toolResults,
    List<ChatAttachmentSummary>? attachments,
    String? messageId,
    String? requestId,
    int? seq,
    String? createdAt,
    String? dayId,
  }) => ChatMessage(
    role: role ?? this.role,
    text: text ?? this.text,
    toolResults: toolResults ?? this.toolResults,
    attachments: attachments ?? this.attachments,
    messageId: messageId ?? this.messageId,
    requestId: requestId ?? this.requestId,
    seq: seq ?? this.seq,
    createdAt: createdAt ?? this.createdAt,
    dayId: dayId ?? this.dayId,
  );
}

class ApprovalRequest {
  const ApprovalRequest({
    required this.id,
    required this.message,
    required this.requestId,
    this.clientId,
    this.originDeviceId,
    this.tool,
    this.capability,
    this.risk,
    this.whatsapp,
  });

  final String id;
  final String message;
  final String requestId;
  final String? clientId;
  final String? originDeviceId;
  final String? tool;
  final String? capability;
  final String? risk;
  final WhatsAppApproval? whatsapp;

  static ApprovalRequest? tryParse(Object? value) {
    if (value is! Map) return null;
    final map = Map<String, dynamic>.from(value);
    if (map['id'] is! String ||
        map['message'] is! String ||
        map['request_id'] is! String) {
      return null;
    }
    return ApprovalRequest(
      id: map['id'] as String,
      message: map['message'] as String,
      requestId: map['request_id'] as String,
      clientId: map['client_id'] is String ? map['client_id'] as String : null,
      originDeviceId: map['origin_device_id'] is String
          ? map['origin_device_id'] as String
          : null,
      tool: map['tool'] is String ? map['tool'] as String : null,
      capability: map['capability'] is String
          ? map['capability'] as String
          : null,
      risk: map['risk'] is String ? map['risk'] as String : null,
      whatsapp: WhatsAppApproval.tryParse(map['whatsapp']),
    );
  }
}

class WhatsAppApproval {
  const WhatsAppApproval({
    required this.kind,
    required this.contactName,
    required this.message,
    this.goal,
    this.maxAutoReplies,
  });

  final String kind;
  final String contactName;
  final String message;
  final String? goal;
  final int? maxAutoReplies;

  static WhatsAppApproval? tryParse(Object? value) {
    if (value is! Map) return null;
    final map = Map<String, dynamic>.from(value);
    final kind = map['kind'];
    if ((kind != 'send' && kind != 'auto_start') ||
        map['contact_name'] is! String ||
        map['message'] is! String) {
      return null;
    }
    return WhatsAppApproval(
      kind: kind as String,
      contactName: map['contact_name'] as String,
      message: map['message'] as String,
      goal: map['goal'] is String ? map['goal'] as String : null,
      maxAutoReplies: map['max_auto_replies'] is int
          ? map['max_auto_replies'] as int
          : null,
    );
  }
}

class DeviceSnapshot {
  const DeviceSnapshot({
    required this.deviceId,
    required this.displayName,
    required this.platform,
    required this.capabilities,
    required this.online,
  });

  final String deviceId;
  final String displayName;
  final String platform;
  final List<String> capabilities;
  final bool online;

  static DeviceSnapshot? tryParse(Object? value) {
    if (value is! Map) return null;
    final map = Map<String, dynamic>.from(value);
    final caps = map['capabilities'];
    if (map['device_id'] is! String ||
        map['display_name'] is! String ||
        map['platform'] is! String ||
        map['online'] is! bool ||
        caps is! List) {
      return null;
    }
    return DeviceSnapshot(
      deviceId: map['device_id'] as String,
      displayName: map['display_name'] as String,
      platform: map['platform'] as String,
      capabilities: caps.whereType<String>().toList(growable: false),
      online: map['online'] as bool,
    );
  }
}

class NcTraceEvent {
  const NcTraceEvent({
    required this.runId,
    required this.dayId,
    required this.seq,
    required this.event,
    required this.metrics,
    this.stage,
    this.status,
    this.title,
    this.detail,
    this.time,
  });

  final String runId;
  final String dayId;
  final int seq;
  final String event;
  final String? stage;
  final String? status;
  final String? title;
  final String? detail;
  final Map<String, Object> metrics;
  final String? time;

  static NcTraceEvent? tryParse(Object? value) {
    if (value is! Map) return null;

    final map = Map<String, dynamic>.from(value);

    final seq = map['seq'];

    if (map['run_id'] is! String ||
        map['day_id'] is! String ||
        seq is! num ||
        map['event'] is! String) {
      return null;
    }

    final metrics = <String, Object>{};

    final rawMetrics = map['metrics'];

    if (rawMetrics is Map) {
      for (final entry in rawMetrics.entries) {
        final key = entry.key.toString();

        final raw = entry.value;

        if (raw is String || raw is num || raw is bool) {
          metrics[key] = raw as Object;
        }
      }
    }

    return NcTraceEvent(
      runId: map['run_id'] as String,
      dayId: map['day_id'] as String,
      seq: seq.toInt(),
      event: map['event'] as String,
      stage: map['stage'] is String ? map['stage'] as String : null,
      status: map['status'] is String ? map['status'] as String : null,
      title: map['title'] is String ? map['title'] as String : null,
      detail: map['detail'] is String ? map['detail'] as String : null,
      metrics: metrics,
      time: map['time'] is String ? map['time'] as String : null,
    );
  }
}

class NcTraceRun {
  const NcTraceRun({
    required this.runId,
    required this.dayId,
    required this.status,
    required this.events,
    this.updatedAt,
  });

  final String runId;
  final String dayId;
  final String status;
  final List<NcTraceEvent> events;
  final String? updatedAt;

  NcTraceRun copyWith({
    String? runId,
    String? dayId,
    String? status,
    List<NcTraceEvent>? events,
    String? updatedAt,
  }) => NcTraceRun(
    runId: runId ?? this.runId,
    dayId: dayId ?? this.dayId,
    status: status ?? this.status,
    events: events ?? this.events,
    updatedAt: updatedAt ?? this.updatedAt,
  );

  static NcTraceRun? tryParseSnapshot(Object? value) {
    if (value is! Map) {
      return null;
    }

    final map = Map<String, dynamic>.from(value);

    final rawEvents = map['events'];

    if (map['run_id'] is! String ||
        map['day_id'] is! String ||
        rawEvents is! List) {
      return null;
    }

    final events =
        rawEvents
            .map(NcTraceEvent.tryParse)
            .whereType<NcTraceEvent>()
            .toList(growable: false)
          ..sort((left, right) => left.seq.compareTo(right.seq));

    return NcTraceRun(
      runId: map['run_id'] as String,
      dayId: map['day_id'] as String,
      status: map['status'] is String ? map['status'] as String : 'running',
      events: events,
      updatedAt: map['updated_at'] is String
          ? map['updated_at'] as String
          : null,
    );
  }
}

class EpisServerConfig {
  const EpisServerConfig({required this.url, required this.token});
  final String url;
  final String token;
  bool get isReady => url.trim().isNotEmpty && token.trim().isNotEmpty;

  EpisServerConfig copyWith({String? url, String? token}) =>
      EpisServerConfig(url: url ?? this.url, token: token ?? this.token);
}

enum EpisConnectionStatus { connecting, online, offline }

enum ChatRole { user, assistant }

class ChatMessage {
  const ChatMessage({
    required this.role,
    required this.text,
    this.toolResults = const [],
  });

  final ChatRole role;
  final String text;
  final List<Map<String, dynamic>> toolResults;
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
  });

  final String id;
  final String message;
  final String requestId;
  final String? clientId;
  final String? originDeviceId;
  final String? tool;
  final String? capability;
  final String? risk;

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

class EpisServerConfig {
  const EpisServerConfig({required this.url, required this.token});
  final String url;
  final String token;
  bool get isReady => url.trim().isNotEmpty && token.trim().isNotEmpty;

  EpisServerConfig copyWith({String? url, String? token}) =>
      EpisServerConfig(url: url ?? this.url, token: token ?? this.token);
}

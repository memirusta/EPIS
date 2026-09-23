import 'dart:async';

import 'package:flutter/foundation.dart';

import 'android_device_agent.dart';
import 'epis_client.dart';
import 'models.dart';
import 'secure_config.dart';

class EpisController extends ChangeNotifier {
  EpisController(this._configStore) {
    _client = EpisClient(
      onPayload: _handlePayload,
      onConnection: _handleConnection,
    );
    _deviceAgent = AndroidDeviceAgent(fallbackClientId: _client.clientId);
  }

  final SecureConfigStore _configStore;
  late final EpisClient _client;
  late final AndroidDeviceAgent _deviceAgent;

  EpisServerConfig _config = const EpisServerConfig(
    url: defaultEpisServerUrl,
    token: '',
  );

  EpisConnectionStatus connection = EpisConnectionStatus.offline;
  String serverVersion = '';
  String? error;
  bool bootstrapped = false;
  bool pickingAttachment = false;
  final List<ChatMessage> messages = [];
  final List<DeviceSnapshot> devices = [];
  final List<ApprovalRequest> approvals = [];
  final List<ChatAttachment> pendingAttachments = [];
  final Set<String> _inFlightRequests = <String>{};
  final Set<String> _approvalSubmitting = <String>{};
  final Map<String, ChatMessage> _pendingUserMessages = {};
  Completer<void>? _deviceRefreshCompleter;

  EpisServerConfig get config => _config;
  bool get hasCredentials => _config.isReady;
  bool get waiting => _inFlightRequests.isNotEmpty;
  ApprovalRequest? get approval => approvals.isEmpty ? null : approvals.first;
  int get activeRequestCount => _inFlightRequests.length;
  bool get pcAgentOnline =>
      devices.any((d) => d.online && d.platform.toLowerCase() == 'windows');

  Future<void> bootstrap() async {
    _config = await _configStore.read();
    bootstrapped = true;
    notifyListeners();
    if (_config.isReady) {
      unawaited(_client.connect(_config));
      unawaited(_deviceAgent.connect(_config));
    }
  }

  String? _normalizeCloudUrl(String raw) {
    final parsed = Uri.tryParse(raw.trim());
    if (parsed == null ||
        parsed.scheme.toLowerCase() != 'wss' ||
        parsed.host.isEmpty ||
        parsed.userInfo.isNotEmpty ||
        parsed.hasQuery ||
        parsed.hasFragment) {
      return null;
    }
    final path = parsed.path.isEmpty || parsed.path == '/' ? '/ws' : parsed.path;
    if (path != '/ws') return null;
    return parsed.replace(path: '/ws').toString();
  }

  Future<void> saveConfig({required String url, required String token}) async {
    final normalizedUrl = _normalizeCloudUrl(url);
    final normalizedToken = token.trim();
    if (normalizedUrl == null) {
      error = 'Cloud EPIS adresi wss://host/ws biçiminde olmalı; URL içinde kullanıcı bilgisi, query veya fragment bulunamaz.';
      notifyListeners();
      return;
    }
    if (normalizedToken.isEmpty) {
      error = 'EPIS token boş olamaz.';
      notifyListeners();
      return;
    }
    final next = EpisServerConfig(url: normalizedUrl, token: normalizedToken);
    try {
      await _configStore.write(next);
    } catch (_) {
      error = 'EPIS bağlantı ayarları güvenli depoya kaydedilemedi.';
      notifyListeners();
      return;
    }
    _config = next;
    error = null;
    notifyListeners();
    unawaited(_client.connect(next));
    unawaited(_deviceAgent.connect(next));
  }

  Future<void> forgetToken() async {
    await _configStore.clearToken();
    _config = _config.copyWith(token: '');
    messages.clear();
    devices.clear();
    approvals.clear();
    pendingAttachments.clear();
    _inFlightRequests.clear();
    _approvalSubmitting.clear();
    _pendingUserMessages.clear();
    error = null;
    await Future.wait([
      _client.disconnect(),
      _deviceAgent.disconnect(),
    ]);
    notifyListeners();
  }

  bool sendMessage(String text) {
    final value = text.trim();
    if ((value.isEmpty && pendingAttachments.isEmpty) ||
        connection != EpisConnectionStatus.online) {
      return false;
    }

    final attachments = List<ChatAttachment>.from(pendingAttachments);
    final requestId = _client.sendChat(value, attachments: attachments);
    if (requestId == null) {
      error = 'Mesaj server bağlantısına yazılamadı.';
      notifyListeners();
      return false;
    }

    final displayText = value.isNotEmpty ? value : 'Ekli dosyayı incele.';
    final optimistic = ChatMessage(
      role: ChatRole.user,
      text: displayText,
      attachments: attachments.map((item) => item.summary).toList(),
    );
    messages.add(optimistic);
    _pendingUserMessages[requestId] = optimistic;
    pendingAttachments.clear();
    _inFlightRequests.add(requestId);
    error = null;
    notifyListeners();
    return true;
  }

  Future<void> pickAttachment() async {
    if (pickingAttachment) return;
    if (pendingAttachments.length >= 3) {
      error = 'Bir mesaja en fazla 3 ek ekleyebilirsin.';
      notifyListeners();
      return;
    }

    pickingAttachment = true;
    error = null;
    notifyListeners();
    try {
      final attachment = await _deviceAgent.pickAttachment();
      if (attachment != null) {
        pendingAttachments.add(attachment);
      }
    } catch (exc) {
      error = 'Dosya seçilemedi: ${exc.toString().replaceFirst('Bad state: ', '')}';
    } finally {
      pickingAttachment = false;
      notifyListeners();
    }
  }

  void removeAttachment(int index) {
    if (index < 0 || index >= pendingAttachments.length) return;
    pendingAttachments.removeAt(index);
    notifyListeners();
  }

  void confirmApproval() {
    final current = approval;
    if (current == null || _approvalSubmitting.contains(current.id)) return;

    final operationId = _client.confirmApproval(current.id);
    if (operationId == null) {
      error = 'Onay server bağlantısına yazılamadı.';
      notifyListeners();
      return;
    }

    _approvalSubmitting.add(current.id);
    error = null;
    notifyListeners();
  }

  void rejectApproval() {
    final current = approval;
    if (current == null || _approvalSubmitting.contains(current.id)) return;

    final operationId = _client.rejectApproval(current.id);
    if (operationId == null) {
      error = 'Red cevabı server bağlantısına yazılamadı.';
      notifyListeners();
      return;
    }

    _approvalSubmitting.add(current.id);
    error = null;
    notifyListeners();
  }

  void newConversation() {
    if (connection != EpisConnectionStatus.online) return;

    final requestId = _client.newConversation();
    if (requestId == null) {
      error = 'Yeni bağlam isteği server bağlantısına yazılamadı.';
      notifyListeners();
      return;
    }

    _inFlightRequests.add(requestId);
    error = null;
    notifyListeners();
  }

  Future<void> refreshDevices() async {
    if (connection != EpisConnectionStatus.online) return;

    final previous = _deviceRefreshCompleter;
    if (previous != null && !previous.isCompleted) previous.complete();
    final completer = Completer<void>();
    _deviceRefreshCompleter = completer;

    if (!_client.requestDevices()) {
      _deviceRefreshCompleter = null;
      error = 'Cihaz listesi isteği server bağlantısına yazılamadı.';
      notifyListeners();
      return;
    }

    try {
      await completer.future.timeout(const Duration(seconds: 5));
    } on TimeoutException {
      if (identical(_deviceRefreshCompleter, completer)) {
        error = 'Cihaz listesi zamanında yenilenemedi.';
        notifyListeners();
      }
    } finally {
      if (identical(_deviceRefreshCompleter, completer)) {
        _deviceRefreshCompleter = null;
      }
    }
  }

  void reconnect() {
    if (_config.isReady) {
      unawaited(_client.connect(_config));
      unawaited(_deviceAgent.connect(_config));
    }
  }

  void _handleConnection(EpisConnectionStatus next) {
    connection = next;
    notifyListeners();
  }

  void _queueApproval(ApprovalRequest request) {
    approvals.removeWhere((item) => item.id == request.id);
    approvals.add(request);
  }

  void _finishRequest(Object? requestId) {
    if (requestId is String) {
      _inFlightRequests.remove(requestId);
      _pendingUserMessages.remove(requestId);
    }
  }

  List<ChatMessage> _parseConversationSnapshot(Object? value) {
    if (value is! List) return const [];
    final result = <ChatMessage>[];
    for (final raw in value) {
      if (raw is! Map) continue;
      final map = Map<String, dynamic>.from(raw);
      final role = map['role'];
      final text = map['text']?.toString() ?? '';
      if (role != 'user' && role != 'assistant') continue;
      final attachments = <ChatAttachmentSummary>[];
      final rawAttachments = map['attachments'];
      if (rawAttachments is List) {
        for (final item in rawAttachments) {
          final parsed = ChatAttachmentSummary.tryParse(item);
          if (parsed != null) attachments.add(parsed);
        }
      }
      if (text.trim().isEmpty && attachments.isEmpty) continue;
      result.add(
        ChatMessage(
          role: role == 'user' ? ChatRole.user : ChatRole.assistant,
          text: text.trim(),
          attachments: attachments,
        ),
      );
    }
    return result;
  }

  void _handlePayload(Map<String, dynamic> payload) {
    switch (payload['type']) {
      case 'connected':
        serverVersion = payload['version']?.toString() ?? '';
        break;

      case 'client.ready':
        connection = EpisConnectionStatus.online;
        error = null;
        break;

      case 'chat.accepted':
      case 'conversation.accepted':
        break;

      case 'conversation.snapshot':
        final snapshot = _parseConversationSnapshot(payload['messages']);
        messages
          ..clear()
          ..addAll(snapshot)
          ..addAll(_pendingUserMessages.values);
        break;

      case 'conversation.live_message':
        final role = payload['role'];
        final text = payload['text']?.toString().trim() ?? '';
        if ((role == 'user' || role == 'assistant') && text.isNotEmpty) {
          final attachments = <ChatAttachmentSummary>[];
          final rawAttachments = payload['attachments'];
          if (rawAttachments is List) {
            for (final item in rawAttachments) {
              final parsed = ChatAttachmentSummary.tryParse(item);
              if (parsed != null) attachments.add(parsed);
            }
          }
          messages.add(
            ChatMessage(
              role: role == 'user' ? ChatRole.user : ChatRole.assistant,
              text: text,
              attachments: attachments,
            ),
          );
        }
        break;

      case 'assistant.message':
        final text = payload['text']?.toString().trim() ?? '';
        final results = <Map<String, dynamic>>[];
        final rawResults = payload['tool_results'];
        if (rawResults is List) {
          for (final result in rawResults) {
            if (result is Map) results.add(Map<String, dynamic>.from(result));
          }
        }
        if (text.isNotEmpty || results.isNotEmpty) {
          messages.add(
            ChatMessage(
              role: ChatRole.assistant,
              text: text,
              toolResults: results,
            ),
          );
        }
        if (payload['confirmation_required'] == true) {
          final parsed = ApprovalRequest.tryParse(payload['approval']);
          if (parsed == null) {
            error = 'Onay isteği okunamadı.';
          } else {
            _queueApproval(parsed);
          }
        }
        _finishRequest(payload['request_id']);
        break;

      case 'proactive.message':
        final text = payload['text']?.toString().trim() ?? '';
        if (text.isNotEmpty) {
          messages.add(ChatMessage(role: ChatRole.assistant, text: text));
        }
        break;

      case 'approval.accepted':
        final approvalId = payload['approval_id'];
        if (approvalId is String) {
          approvals.removeWhere((item) => item.id == approvalId);
          _approvalSubmitting.remove(approvalId);
        }
        final requestId = payload['request_id'];
        if (requestId is String && requestId.isNotEmpty) {
          _inFlightRequests.add(requestId);
        }
        break;

      case 'devices.snapshot':
        final raw = payload['devices'];
        if (raw is List) {
          devices
            ..clear()
            ..addAll(
              raw.map(DeviceSnapshot.tryParse).whereType<DeviceSnapshot>(),
            );
        }
        final refresh = _deviceRefreshCompleter;
        if (refresh != null && !refresh.isCompleted) refresh.complete();
        if (error == 'Cihaz listesi zamanında yenilenemedi.' ||
            error == 'Cihaz listesi isteği server bağlantısına yazılamadı.') {
          error = null;
        }
        break;

      case 'conversation.reset':
        messages.clear();
        approvals.clear();
        pendingAttachments.clear();
        _approvalSubmitting.clear();
        _inFlightRequests.clear();
        _pendingUserMessages.clear();
        error = null;
        break;

      case 'error':
        _finishRequest(payload['request_id']);
        final approvalId = payload['approval_id'];
        if (approvalId is String) {
          _approvalSubmitting.remove(approvalId);
        }
        error =
            payload['detail']?.toString() ??
            payload['error']?.toString() ??
            'Bilinmeyen EPIS server hatası.';
        break;

      case 'pong':
        break;
    }
    notifyListeners();
  }

  @override
  void dispose() {
    final refresh = _deviceRefreshCompleter;
    if (refresh != null && !refresh.isCompleted) refresh.complete();
    unawaited(_client.dispose());
    unawaited(_deviceAgent.dispose());
    super.dispose();
  }
}

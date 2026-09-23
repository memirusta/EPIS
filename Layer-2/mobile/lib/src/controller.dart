import 'dart:async';

import 'package:flutter/foundation.dart';

import 'epis_client.dart';
import 'models.dart';
import 'secure_config.dart';

class EpisController extends ChangeNotifier {
  EpisController(this._configStore) {
    _client = EpisClient(
      onPayload: _handlePayload,
      onConnection: _handleConnection,
    );
  }

  final SecureConfigStore _configStore;
  late final EpisClient _client;

  EpisServerConfig _config = const EpisServerConfig(
    url: defaultEpisServerUrl,
    token: '',
  );

  EpisConnectionStatus connection = EpisConnectionStatus.offline;
  String serverVersion = '';
  String? error;
  bool bootstrapped = false;
  final List<ChatMessage> messages = [];
  final List<DeviceSnapshot> devices = [];
  final List<ApprovalRequest> approvals = [];
  final Set<String> _inFlightRequests = <String>{};
  final Set<String> _approvalSubmitting = <String>{};
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
    if (_config.isReady) unawaited(_client.connect(_config));
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
  }

  Future<void> forgetToken() async {
    await _configStore.clearToken();
    _config = _config.copyWith(token: '');
    messages.clear();
    devices.clear();
    approvals.clear();
    _inFlightRequests.clear();
    _approvalSubmitting.clear();
    error = null;
    await _client.disconnect();
    notifyListeners();
  }

  bool sendMessage(String text) {
    final value = text.trim();
    if (value.isEmpty || connection != EpisConnectionStatus.online) {
      return false;
    }

    final requestId = _client.sendChat(value);
    if (requestId == null) {
      error = 'Mesaj server bağlantısına yazılamadı.';
      notifyListeners();
      return false;
    }

    messages.add(ChatMessage(role: ChatRole.user, text: value));
    _inFlightRequests.add(requestId);
    error = null;
    notifyListeners();
    return true;
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
    if (_config.isReady) unawaited(_client.connect(_config));
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
    }
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
        _approvalSubmitting.clear();
        _inFlightRequests.clear();
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
    super.dispose();
  }
}

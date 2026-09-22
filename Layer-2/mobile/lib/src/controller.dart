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
  bool waiting = false;
  bool bootstrapped = false;
  final List<ChatMessage> messages = [];
  final List<DeviceSnapshot> devices = [];
  ApprovalRequest? approval;

  EpisServerConfig get config => _config;
  bool get hasCredentials => _config.isReady;
  bool get pcAgentOnline =>
      devices.any((d) => d.online && d.platform.toLowerCase() == 'windows');

  Future<void> bootstrap() async {
    _config = await _configStore.read();
    bootstrapped = true;
    notifyListeners();
    if (_config.isReady) unawaited(_client.connect(_config));
  }

  Future<void> saveConfig({required String url, required String token}) async {
    final normalizedUrl = url.trim();
    final normalizedToken = token.trim();
    if (!normalizedUrl.startsWith('wss://')) {
      error = 'Cloud EPIS bağlantısı wss:// kullanmalı.';
      notifyListeners();
      return;
    }
    if (normalizedToken.isEmpty) {
      error = 'EPIS token boş olamaz.';
      notifyListeners();
      return;
    }
    final next = EpisServerConfig(url: normalizedUrl, token: normalizedToken);
    await _configStore.write(next);
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
    approval = null;
    waiting = false;
    error = null;
    await _client.disconnect();
    notifyListeners();
  }

  void sendMessage(String text) {
    final value = text.trim();
    if (value.isEmpty || waiting || connection != EpisConnectionStatus.online) {
      return;
    }
    messages.add(ChatMessage(role: ChatRole.user, text: value));
    waiting = true;
    error = null;
    notifyListeners();
    _client.sendChat(value);
  }

  void confirmApproval() {
    final current = approval;
    if (current == null || waiting) return;
    approval = null;
    waiting = true;
    notifyListeners();
    _client.confirmApproval(current.id);
  }

  void rejectApproval() {
    final current = approval;
    if (current == null || waiting) return;
    approval = null;
    waiting = true;
    notifyListeners();
    _client.rejectApproval(current.id);
  }

  void newConversation() {
    if (waiting || connection != EpisConnectionStatus.online) return;
    waiting = true;
    approval = null;
    error = null;
    notifyListeners();
    _client.newConversation();
  }

  void refreshDevices() {
    if (connection == EpisConnectionStatus.online) _client.requestDevices();
  }

  void reconnect() {
    if (_config.isReady) unawaited(_client.connect(_config));
  }

  void _handleConnection(EpisConnectionStatus next) {
    connection = next;
    if (next != EpisConnectionStatus.online) waiting = false;
    notifyListeners();
  }

  void _handlePayload(Map<String, dynamic> payload) {
    switch (payload['type']) {
      case 'connected':
        serverVersion = payload['version']?.toString() ?? '';
        connection = EpisConnectionStatus.online;
        error = null;
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
          approval = ApprovalRequest.tryParse(payload['approval']);
          if (approval == null) error = 'Onay isteği okunamadı.';
        }
        waiting = false;
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
        break;
      case 'conversation.reset':
        messages.clear();
        approval = null;
        waiting = false;
        error = null;
        break;
      case 'error':
        waiting = false;
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
    unawaited(_client.dispose());
    super.dispose();
  }
}

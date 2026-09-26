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
  NcTraceRun? nightlyTrace;
  final Set<String> _inFlightRequests = <String>{};
  final Set<String> _approvalSubmitting = <String>{};
  final Set<String> _externalReplyOutreachIds = <String>{};
  final Map<String, ChatMessage> _pendingUserMessages = {};
  String? _activeDayId;
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
    final path = parsed.path.isEmpty || parsed.path == '/'
        ? '/ws'
        : parsed.path;
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
    nightlyTrace = null;
    _inFlightRequests.clear();
    _approvalSubmitting.clear();
    _externalReplyOutreachIds.clear();
    _pendingUserMessages.clear();
    error = null;
    await Future.wait([_client.disconnect(), _deviceAgent.disconnect()]);
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
      requestId: requestId,
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
      error =
          'Dosya seçilemedi: ${exc.toString().replaceFirst('Bad state: ', '')}';
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

  ChatMessage? _parseConversationMessage(Object? value) {
    if (value is! Map) return null;
    final map = Map<String, dynamic>.from(value);
    final role = map['role'];
    final text = map['text']?.toString() ?? '';
    if (role != 'user' && role != 'assistant') return null;
    final attachments = <ChatAttachmentSummary>[];
    final rawAttachments = map['attachments'];
    if (rawAttachments is List) {
      for (final item in rawAttachments) {
        final parsed = ChatAttachmentSummary.tryParse(item);
        if (parsed != null) attachments.add(parsed);
      }
    }
    if (text.trim().isEmpty && attachments.isEmpty) return null;
    final seqValue = map['seq'];
    return ChatMessage(
      role: role == 'user' ? ChatRole.user : ChatRole.assistant,
      text: text.trim(),
      attachments: attachments,
      messageId: map['message_id'] is String
          ? map['message_id'] as String
          : null,
      requestId: map['request_id'] is String
          ? map['request_id'] as String
          : null,
      seq: seqValue is num ? seqValue.toInt() : null,
      createdAt: map['created_at'] is String
          ? map['created_at'] as String
          : null,
      dayId: map['day_id'] is String ? map['day_id'] as String : null,
    );
  }

  List<ChatMessage> _parseConversationSnapshot(Object? value) {
    if (value is! List) return const [];
    return value
        .map(_parseConversationMessage)
        .whereType<ChatMessage>()
        .toList(growable: false);
  }

  bool _sameCanonicalMessage(ChatMessage left, ChatMessage right) {
    if (left.messageId != null && right.messageId != null) {
      return left.messageId == right.messageId;
    }
    if (left.requestId != null &&
        right.requestId != null &&
        left.role == right.role) {
      return left.requestId == right.requestId;
    }
    return false;
  }

  void _mergeCanonicalMessage(ChatMessage incoming) {
    if (incoming.dayId != null &&
        _activeDayId != null &&
        incoming.dayId != _activeDayId) {
      _activeDayId = incoming.dayId;
      messages
        ..clear()
        ..add(incoming);
      return;
    }
    _activeDayId ??= incoming.dayId;
    final index = messages.indexWhere(
      (item) => _sameCanonicalMessage(item, incoming),
    );
    if (index < 0) {
      messages.add(incoming);
      return;
    }
    final current = messages[index];
    messages[index] = current.copyWith(
      role: incoming.role,
      text: incoming.text,
      toolResults: incoming.toolResults.isNotEmpty
          ? incoming.toolResults
          : current.toolResults,
      attachments: incoming.attachments.isNotEmpty
          ? incoming.attachments
          : current.attachments,
      messageId: incoming.messageId,
      requestId: incoming.requestId,
      seq: incoming.seq,
      createdAt: incoming.createdAt,
      dayId: incoming.dayId,
    );
  }

  String _traceRunStatus(NcTraceEvent event, String current) {
    if (event.event == 'run.failed') {
      return 'failed';
    }

    if (event.event == 'run.completed') {
      return event.status ?? 'success';
    }

    if (event.event == 'run.started') {
      return 'running';
    }

    return current;
  }

  void _applyNcTraceEvent(NcTraceEvent event) {
    final current = nightlyTrace;

    final base = current != null && current.runId == event.runId
        ? current
        : NcTraceRun(
            runId: event.runId,
            dayId: event.dayId,
            status: 'running',
            events: const [],
          );

    final bySeq = <int, NcTraceEvent>{
      for (final item in base.events) item.seq: item,
    };

    bySeq[event.seq] = event;

    final events = bySeq.values.toList()
      ..sort((left, right) => left.seq.compareTo(right.seq));

    nightlyTrace = base.copyWith(
      dayId: event.dayId,
      status: _traceRunStatus(event, base.status),
      events: events,
      updatedAt: event.time ?? base.updatedAt,
    );
  }

  void _handleExternalReply(Map<String, dynamic> payload) {
    if (payload['provider'] != 'whatsapp') return;

    final outreachId = payload['outreach_id'];
    final content = payload['content'];
    final receivedAt = payload['received_at'];
    if (outreachId is! String ||
        outreachId.trim().isEmpty ||
        content is! String ||
        content.trim().isEmpty ||
        receivedAt is! String ||
        receivedAt.trim().isEmpty ||
        _externalReplyOutreachIds.contains(outreachId)) {
      return;
    }

    _externalReplyOutreachIds.add(outreachId);
    final rawName = payload['contact_name'];
    final sender = rawName is String && rawName.trim().isNotEmpty
        ? rawName.trim()
        : 'Bir kişi';
    _mergeCanonicalMessage(
      ChatMessage(
        role: ChatRole.assistant,
        text: "$sender WhatsApp'tan cevap verdi:\n${content.trim()}",
        messageId: 'external.reply:$outreachId',
        createdAt: receivedAt,
      ),
    );
  }

  void _handlePayload(Map<String, dynamic> payload) {
    switch (payload['type']) {
      case 'connected':
        serverVersion = payload['version']?.toString() ?? '';
        if (payload['day_id'] is String) {
          _activeDayId = payload['day_id'] as String;
        }
        break;

      case 'client.ready':
        connection = EpisConnectionStatus.online;
        error = null;
        break;

      case 'nc.trace.snapshot':
        final snapshot = NcTraceRun.tryParseSnapshot(payload);

        if (snapshot != null) {
          nightlyTrace = snapshot;
        }
        break;

      case 'nc.trace':
        final event = NcTraceEvent.tryParse(payload);

        if (event != null) {
          _applyNcTraceEvent(event);
        }
        break;

      case 'chat.accepted':
        final requestId = payload['request_id'];
        final canonical = _parseConversationMessage(payload['message']);
        if (requestId is String && canonical != null) {
          if (canonical.dayId != null &&
              _activeDayId != null &&
              canonical.dayId != _activeDayId) {
            _activeDayId = canonical.dayId;
            messages
              ..clear()
              ..add(canonical);
          } else {
            _activeDayId ??= canonical.dayId;
            final index = messages.indexWhere(
              (item) =>
                  item.role == ChatRole.user && item.requestId == requestId,
            );
            if (index >= 0) {
              messages[index] = messages[index].copyWith(
                messageId: canonical.messageId,
                requestId: canonical.requestId,
                seq: canonical.seq,
                createdAt: canonical.createdAt,
                attachments: canonical.attachments,
                dayId: canonical.dayId,
              );
            }
          }
          _pendingUserMessages[requestId] = canonical;
        }
        break;

      case 'conversation.accepted':
        break;

      case 'conversation.snapshot':
        final snapshotDay = payload['day_id'] is String
            ? payload['day_id'] as String
            : _activeDayId;
        _activeDayId = snapshotDay;
        final snapshot = _parseConversationSnapshot(payload['messages']);
        final merged = <ChatMessage>[...snapshot];
        for (final pending in _pendingUserMessages.values) {
          final exists = merged.any(
            (item) => _sameCanonicalMessage(item, pending),
          );
          if (!exists) merged.add(pending);
        }
        messages
          ..clear()
          ..addAll(merged);
        break;

      case 'conversation.live_message':
        final incoming = _parseConversationMessage(payload);
        if (incoming != null) {
          if (incoming.dayId != null &&
              _activeDayId != null &&
              incoming.dayId != _activeDayId) {
            _activeDayId = incoming.dayId;
            messages
              ..clear()
              ..add(incoming);
          } else {
            _activeDayId ??= incoming.dayId;
            _mergeCanonicalMessage(incoming);
          }
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
          final seqValue = payload['seq'];
          _mergeCanonicalMessage(
            ChatMessage(
              role: ChatRole.assistant,
              text: text,
              toolResults: results,
              messageId: payload['message_id'] is String
                  ? payload['message_id'] as String
                  : null,
              requestId: payload['request_id'] is String
                  ? payload['request_id'] as String
                  : null,
              seq: seqValue is num ? seqValue.toInt() : null,
              createdAt: payload['created_at'] is String
                  ? payload['created_at'] as String
                  : null,
              dayId: payload['day_id'] is String
                  ? payload['day_id'] as String
                  : null,
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
          final seqValue = payload['seq'];
          _mergeCanonicalMessage(
            ChatMessage(
              role: ChatRole.assistant,
              text: text,
              messageId: payload['message_id'] is String
                  ? payload['message_id'] as String
                  : null,
              requestId: payload['request_id'] is String
                  ? payload['request_id'] as String
                  : null,
              seq: seqValue is num ? seqValue.toInt() : null,
              createdAt: payload['created_at'] is String
                  ? payload['created_at'] as String
                  : null,
              dayId: payload['day_id'] is String
                  ? payload['day_id'] as String
                  : null,
            ),
          );
        }
        break;

      case 'external.reply':
        _handleExternalReply(payload);
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
        _finishRequest(payload['request_id']);
        if (payload['preserved_daily_transcript'] != true) {
          messages.clear();
          pendingAttachments.clear();
          _inFlightRequests.clear();
          _pendingUserMessages.clear();
        }
        approvals.clear();
        _approvalSubmitting.clear();
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

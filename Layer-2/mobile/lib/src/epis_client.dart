import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:web_socket_channel/web_socket_channel.dart';

import 'models.dart';

typedef EpisPayloadHandler = void Function(Map<String, dynamic> payload);
typedef EpisConnectionHandler = void Function(EpisConnectionStatus status);

class EpisClient {
  EpisClient({required this.onPayload, required this.onConnection})
      : _clientId =
            'mobile-${DateTime.now().microsecondsSinceEpoch.toRadixString(36)}';

  final EpisPayloadHandler onPayload;
  final EpisConnectionHandler onConnection;
  final String _clientId;

  WebSocketChannel? _channel;
  StreamSubscription<Object?>? _subscription;
  Timer? _reconnectTimer;
  Timer? _heartbeatTimer;
  Timer? _deviceRefreshTimer;
  EpisServerConfig? _config;
  bool _disposed = false;
  bool _manualDisconnect = false;
  bool _ready = false;
  int _generation = 0;
  int _requestCounter = 0;
  int _reconnectAttempt = 0;
  final math.Random _random = math.Random();

  String get clientId => _clientId;

  String _nextId(String prefix) {
    _requestCounter += 1;
    return '$prefix:${DateTime.now().microsecondsSinceEpoch}:$_requestCounter';
  }

  Future<void> connect(EpisServerConfig config) async {
    _config = config;
    _manualDisconnect = false;
    _ready = false;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;

    _generation += 1;
    final generation = _generation;
    await _closeSocket();

    if (!config.isReady || _disposed) {
      onConnection(EpisConnectionStatus.offline);
      return;
    }

    onConnection(EpisConnectionStatus.connecting);

    try {
      final channel = WebSocketChannel.connect(
        Uri.parse(config.url),
        protocols: <String>['epis', config.token],
      );
      _channel = channel;
      await channel.ready;

      if (_disposed || generation != _generation) {
        await channel.sink.close();
        return;
      }

      _subscription = channel.stream.listen(
        (message) {
          if (generation != _generation) return;
          try {
            final decoded = jsonDecode(message as String);
            if (decoded is! Map) return;

            final payload = Map<String, dynamic>.from(decoded);
            if (payload['type'] == 'client.ready') {
              _ready = true;
              _reconnectAttempt = 0;
              onConnection(EpisConnectionStatus.online);
              _startOnlineTimers(generation);
            }
            onPayload(payload);
          } catch (_) {
            onPayload({'type': 'error', 'error': 'invalid_server_message'});
          }
        },
        onError: (_) => _handleDisconnect(generation),
        onDone: () => _handleDisconnect(generation),
        cancelOnError: true,
      );

      channel.sink.add(
        jsonEncode({
          'type': 'client.hello',
          'version': 2,
          'client_id': _clientId,
          'client_type': 'mobile',
        }),
      );
    } catch (_) {
      _handleDisconnect(generation);
    }
  }

  void _startOnlineTimers(int generation) {
    if (generation != _generation || !_ready) return;

    _heartbeatTimer?.cancel();
    _heartbeatTimer = Timer.periodic(
      const Duration(seconds: 20),
      (_) => send({'type': 'ping'}),
    );

    _deviceRefreshTimer?.cancel();
    // Device connect/disconnect snapshots are pushed by the server.  This is
    // only a low-frequency recovery poll for missed events.
    _deviceRefreshTimer = Timer.periodic(
      const Duration(seconds: 30),
      (_) => requestDevices(),
    );

    requestDevices();
  }

  void _handleDisconnect(int generation) {
    if (_disposed || generation != _generation) return;
    _channel = null;
    _ready = false;
    _heartbeatTimer?.cancel();
    _deviceRefreshTimer?.cancel();
    onConnection(EpisConnectionStatus.offline);
    if (_manualDisconnect) return;

    _reconnectTimer?.cancel();
    final exponent = _reconnectAttempt > 5 ? 5 : _reconnectAttempt;
    final baseSeconds = 1 << exponent;
    final delaySeconds = baseSeconds > 30 ? 30 : baseSeconds;
    final jitterMs = _random.nextInt(501);
    _reconnectAttempt += 1;
    _reconnectTimer = Timer(
      Duration(seconds: delaySeconds, milliseconds: jitterMs),
      () {
        final config = _config;
        if (config != null && !_disposed && !_manualDisconnect) {
          unawaited(connect(config));
        }
      },
    );
  }

  bool send(Map<String, dynamic> payload) {
    final channel = _channel;
    if (channel == null || !_ready) return false;
    try {
      channel.sink.add(jsonEncode(payload));
      return true;
    } catch (_) {
      return false;
    }
  }

  String? sendChat(String text) {
    final requestId = _nextId('chat');
    final ok = send({
      'type': 'chat.send',
      'request_id': requestId,
      'origin_device_id': 'mobile:$_clientId',
      'text': text.trim(),
    });
    return ok ? requestId : null;
  }

  String? confirmApproval(String id) {
    final operationId = _nextId('approval');
    final ok = send({
      'type': 'approval.confirm',
      'approval_id': id,
      'operation_id': operationId,
    });
    return ok ? operationId : null;
  }

  String? rejectApproval(String id) {
    final operationId = _nextId('approval');
    final ok = send({
      'type': 'approval.reject',
      'approval_id': id,
      'operation_id': operationId,
    });
    return ok ? operationId : null;
  }

  String? newConversation() {
    final requestId = _nextId('conversation');
    final ok = send({
      'type': 'conversation.new',
      'request_id': requestId,
    });
    return ok ? requestId : null;
  }

  bool requestDevices() => send({'type': 'devices.get'});

  Future<void> disconnect() async {
    _manualDisconnect = true;
    _ready = false;
    _generation += 1;
    _reconnectTimer?.cancel();
    onConnection(EpisConnectionStatus.offline);
    await _closeSocket();
  }

  Future<void> _closeSocket() async {
    _heartbeatTimer?.cancel();
    _deviceRefreshTimer?.cancel();
    await _subscription?.cancel();
    _subscription = null;
    final channel = _channel;
    _channel = null;
    _ready = false;
    if (channel != null) {
      try {
        await channel.sink.close();
      } catch (_) {}
    }
  }

  Future<void> dispose() async {
    _disposed = true;
    _manualDisconnect = true;
    _ready = false;
    _reconnectTimer?.cancel();
    await _closeSocket();
  }
}

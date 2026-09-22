import 'dart:async';
import 'dart:convert';

import 'package:web_socket_channel/web_socket_channel.dart';

import 'models.dart';

typedef EpisPayloadHandler = void Function(Map<String, dynamic> payload);
typedef EpisConnectionHandler = void Function(EpisConnectionStatus status);

class EpisClient {
  EpisClient({required this.onPayload, required this.onConnection});

  final EpisPayloadHandler onPayload;
  final EpisConnectionHandler onConnection;

  WebSocketChannel? _channel;
  StreamSubscription<Object?>? _subscription;
  Timer? _reconnectTimer;
  Timer? _heartbeatTimer;
  Timer? _deviceRefreshTimer;
  EpisServerConfig? _config;
  bool _disposed = false;
  bool _manualDisconnect = false;
  int _generation = 0;

  Future<void> connect(EpisServerConfig config) async {
    _config = config;
    _manualDisconnect = false;
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

      onConnection(EpisConnectionStatus.online);
      _subscription = channel.stream.listen(
        (message) {
          if (generation != _generation) return;
          try {
            final decoded = jsonDecode(message as String);
            if (decoded is Map) {
              onPayload(Map<String, dynamic>.from(decoded));
            }
          } catch (_) {
            onPayload({'type': 'error', 'error': 'invalid_server_message'});
          }
        },
        onError: (_) => _handleDisconnect(generation),
        onDone: () => _handleDisconnect(generation),
        cancelOnError: true,
      );

      _heartbeatTimer?.cancel();
      _heartbeatTimer = Timer.periodic(
        const Duration(seconds: 20),
        (_) => send({'type': 'ping'}),
      );
      _deviceRefreshTimer?.cancel();
      _deviceRefreshTimer = Timer.periodic(
        const Duration(seconds: 5),
        (_) => requestDevices(),
      );
      requestDevices();
    } catch (_) {
      _handleDisconnect(generation);
    }
  }

  void _handleDisconnect(int generation) {
    if (_disposed || generation != _generation) return;
    _channel = null;
    _heartbeatTimer?.cancel();
    _deviceRefreshTimer?.cancel();
    onConnection(EpisConnectionStatus.offline);
    if (_manualDisconnect) return;
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(const Duration(seconds: 2), () {
      final config = _config;
      if (config != null && !_disposed && !_manualDisconnect) {
        unawaited(connect(config));
      }
    });
  }

  void send(Map<String, dynamic> payload) {
    final channel = _channel;
    if (channel != null) channel.sink.add(jsonEncode(payload));
  }

  void sendChat(String text) =>
      send({'type': 'chat.send', 'text': text.trim()});
  void confirmApproval(String id) =>
      send({'type': 'approval.confirm', 'approval_id': id});
  void rejectApproval(String id) =>
      send({'type': 'approval.reject', 'approval_id': id});
  void newConversation() => send({'type': 'conversation.new'});
  void requestDevices() => send({'type': 'devices.get'});

  Future<void> disconnect() async {
    _manualDisconnect = true;
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
    if (channel != null) {
      try {
        await channel.sink.close();
      } catch (_) {}
    }
  }

  Future<void> dispose() async {
    _disposed = true;
    _manualDisconnect = true;
    _reconnectTimer?.cancel();
    await _closeSocket();
  }
}

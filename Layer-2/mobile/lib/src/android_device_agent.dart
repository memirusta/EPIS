import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:flutter/services.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import 'models.dart';

class AndroidDeviceAgent {
  AndroidDeviceAgent({required this.fallbackClientId});

  final String fallbackClientId;
  static const MethodChannel _native = MethodChannel(
    'com.epis.epis_mobile/device',
  );

  WebSocketChannel? _channel;
  StreamSubscription<Object?>? _subscription;
  Timer? _reconnectTimer;
  Timer? _heartbeatTimer;
  EpisServerConfig? _config;
  bool _disposed = false;
  bool _manualDisconnect = false;
  int _generation = 0;
  int _reconnectAttempt = 0;
  final math.Random _random = math.Random();
  final Map<String, Map<String, dynamic>> _receipts = {};

  Future<void> connect(EpisServerConfig config) async {
    _config = config;
    _manualDisconnect = false;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _generation += 1;
    final generation = _generation;
    await _closeSocket();

    if (!config.isReady || _disposed) return;

    try {
      final descriptor = await _deviceDescriptor();
      final uri = Uri.parse(config.url).replace(path: '/device/ws');
      final channel = WebSocketChannel.connect(
        uri,
        protocols: <String>['epis-device', config.token],
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
          _handleMessage(message);
        },
        onError: (_) => _handleDisconnect(generation),
        onDone: () => _handleDisconnect(generation),
        cancelOnError: true,
      );

      channel.sink.add(
        jsonEncode({
          'type': 'device.hello',
          'version': 1,
          'device': {
            'device_id': descriptor['device_id'],
            'display_name': descriptor['display_name'],
            'platform': 'android',
            'capabilities': const ['phone.call'],
          },
        }),
      );
    } catch (_) {
      _handleDisconnect(generation);
    }
  }

  Future<Map<String, String>> _deviceDescriptor() async {
    try {
      final raw = await _native.invokeMapMethod<String, dynamic>(
        'getDeviceDescriptor',
      );
      final deviceId = raw?['device_id']?.toString().trim() ?? '';
      final displayName = raw?['display_name']?.toString().trim() ?? '';
      if (deviceId.isNotEmpty && displayName.isNotEmpty) {
        return {
          'device_id': deviceId,
          'display_name': displayName,
        };
      }
    } catch (_) {}
    final safeFallback = fallbackClientId.replaceAll(
      RegExp(r'[^a-zA-Z0-9._-]'),
      '-',
    );
    return {
      'device_id': 'android-$safeFallback',
      'display_name': 'Android Phone',
    };
  }

  Future<ChatAttachment?> pickAttachment() async {
    try {
      final raw = await _native.invokeMapMethod<String, dynamic>(
        'pickAttachment',
      );
      if (raw == null) return null;
      return ChatAttachment.tryParseNative(raw);
    } on PlatformException catch (exc) {
      throw StateError(exc.message ?? exc.code);
    }
  }

  void _handleMessage(Object? message) {
    Map<String, dynamic> payload;
    try {
      final decoded = jsonDecode(message as String);
      if (decoded is! Map) return;
      payload = Map<String, dynamic>.from(decoded);
    } catch (_) {
      return;
    }

    if (payload['type'] == 'device.connected') {
      _reconnectAttempt = 0;
      _heartbeatTimer?.cancel();
      _heartbeatTimer = Timer.periodic(
        const Duration(seconds: 20),
        (_) => _send({'type': 'device.pong'}),
      );
      return;
    }

    if (payload['type'] != 'device.execute') return;
    unawaited(_execute(payload));
  }

  Future<void> _execute(Map<String, dynamic> payload) async {
    final requestId = payload['id'];
    if (requestId is! String || requestId.isEmpty) return;

    final cached = _receipts[requestId];
    if (cached != null) {
      _send({'type': 'device.result', 'id': requestId, 'result': cached});
      return;
    }

    Map<String, dynamic> result;
    final deadline = payload['deadline'];
    final deadlineSeconds = deadline is num ? deadline.toDouble() : 0.0;
    final nowSeconds = DateTime.now().millisecondsSinceEpoch / 1000.0;

    if (payload['version'] != 1 ||
        deadlineSeconds <= nowSeconds ||
        deadlineSeconds > nowSeconds + 65.0) {
      result = {'ok': false, 'error': 'expired_or_invalid_device_command'};
    } else if (payload['capability'] != 'phone.call') {
      result = {'ok': false, 'error': 'capability_not_supported'};
    } else if (payload['confirmed'] != true) {
      result = {'ok': false, 'error': 'device_policy_requires_core_approval'};
    } else {
      final rawArguments = payload['arguments'];
      if (rawArguments is! Map) {
        result = {'ok': false, 'error': 'invalid_phone_call_arguments'};
      } else {
        result = await _placeCall(Map<String, dynamic>.from(rawArguments));
      }
    }

    if (_receipts.length >= 256) {
      _receipts.remove(_receipts.keys.first);
    }
    _receipts[requestId] = result;
    _send({'type': 'device.result', 'id': requestId, 'result': result});
  }

  Future<Map<String, dynamic>> _placeCall(
    Map<String, dynamic> arguments,
  ) async {
    final number = arguments['number']?.toString().trim() ?? '';
    final contact = arguments['contact']?.toString().trim() ?? '';
    if ((number.isEmpty && contact.isEmpty) ||
        (number.isNotEmpty && contact.isNotEmpty)) {
      return {
        'ok': false,
        'error': 'provide_exactly_one_of_number_or_contact',
      };
    }

    try {
      final raw = await _native.invokeMapMethod<String, dynamic>(
        'placeCall',
        {
          if (number.isNotEmpty) 'number': number,
          if (contact.isNotEmpty) 'contact': contact,
        },
      );
      if (raw == null) {
        return {'ok': false, 'error': 'empty_native_phone_result'};
      }
      final result = Map<String, dynamic>.from(raw);
      if (result['ok'] is! bool) {
        return {'ok': false, 'error': 'invalid_native_phone_result'};
      }
      return result;
    } on PlatformException catch (exc) {
      return {
        'ok': false,
        'error': exc.code,
        if ((exc.message ?? '').isNotEmpty) 'detail': exc.message,
      };
    } catch (_) {
      return {
        'ok': false,
        'outcome': 'unknown',
        'error': 'phone_call_native_bridge_failed',
      };
    }
  }

  bool _send(Map<String, dynamic> payload) {
    final channel = _channel;
    if (channel == null) return false;
    try {
      channel.sink.add(jsonEncode(payload));
      return true;
    } catch (_) {
      return false;
    }
  }

  void _handleDisconnect(int generation) {
    if (_disposed || generation != _generation) return;
    _heartbeatTimer?.cancel();
    _channel = null;
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

  Future<void> disconnect() async {
    _manualDisconnect = true;
    _generation += 1;
    _reconnectTimer?.cancel();
    await _closeSocket();
  }

  Future<void> _closeSocket() async {
    _heartbeatTimer?.cancel();
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
    _generation += 1;
    _reconnectTimer?.cancel();
    await _closeSocket();
  }
}

import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import 'models.dart';

const defaultEpisServerUrl = 'wss://epis-emir-38541be39a7b.herokuapp.com/ws';

class SecureConfigStore {
  static const _urlKey = 'epis.server.url';
  static const _tokenKey = 'epis.server.token';
  final FlutterSecureStorage _storage = FlutterSecureStorage();

  Future<EpisServerConfig> read() async {
    final url = await _storage.read(key: _urlKey);
    final token = await _storage.read(key: _tokenKey);
    return EpisServerConfig(
      url: (url == null || url.trim().isEmpty)
          ? defaultEpisServerUrl
          : url.trim(),
      token: token?.trim() ?? '',
    );
  }

  Future<void> write(EpisServerConfig config) async {
    await _storage.write(key: _urlKey, value: config.url.trim());
    await _storage.write(key: _tokenKey, value: config.token.trim());
  }

  Future<void> clearToken() => _storage.delete(key: _tokenKey);
}

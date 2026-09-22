import 'package:flutter/material.dart';

import '../controller.dart';
import '../models.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({
    super.key,
    required this.controller,
    this.setupMode = false,
  });
  final EpisController controller;
  final bool setupMode;

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final TextEditingController _url;
  late final TextEditingController _token;
  bool _obscure = true;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    _url = TextEditingController(text: widget.controller.config.url);
    _token = TextEditingController(text: widget.controller.config.token);
  }

  @override
  void dispose() {
    _url.dispose();
    _token.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    setState(() => _saving = true);
    await widget.controller.saveConfig(url: _url.text, token: _token.text);
    if (mounted) setState(() => _saving = false);
  }

  @override
  Widget build(BuildContext context) {
    final c = widget.controller;
    final (status, color) = switch (c.connection) {
      EpisConnectionStatus.online => (
        'Server ${c.serverVersion} bağlı',
        const Color(0xFF62D99A),
      ),
      EpisConnectionStatus.connecting => (
        'Bağlanıyor',
        const Color(0xFFD5AA55),
      ),
      EpisConnectionStatus.offline => ('Offline', const Color(0xFFDF7777)),
    };

    return Scaffold(
      backgroundColor: widget.setupMode
          ? const Color(0xFF090A0E)
          : Colors.transparent,
      appBar: widget.setupMode
          ? AppBar(title: const Text('EPIS Mobile kurulumu'))
          : null,
      body: ListView(
        padding: const EdgeInsets.all(18),
        children: [
          if (!widget.setupMode)
            const Text(
              'Settings',
              style: TextStyle(fontSize: 28, fontWeight: FontWeight.w700),
            ),
          const SizedBox(height: 8),
          const Text(
            'Cloud Core bağlantısı. Token Android secure storage içinde tutulur.',
            style: TextStyle(fontSize: 11, color: Color(0xFF777A85)),
          ),
          const SizedBox(height: 20),
          TextField(
            controller: _url,
            autocorrect: false,
            keyboardType: TextInputType.url,
            decoration: const InputDecoration(
              labelText: 'Server URL',
              filled: true,
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _token,
            obscureText: _obscure,
            autocorrect: false,
            enableSuggestions: false,
            decoration: InputDecoration(
              labelText: 'EPIS token',
              filled: true,
              suffixIcon: IconButton(
                onPressed: () => setState(() => _obscure = !_obscure),
                icon: Icon(
                  _obscure
                      ? Icons.visibility_outlined
                      : Icons.visibility_off_outlined,
                ),
              ),
            ),
          ),
          const SizedBox(height: 14),
          FilledButton.icon(
            onPressed: _saving ? null : _save,
            icon: _saving
                ? const SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.lock_outline),
            label: Text(
              widget.setupMode
                  ? 'Kaydet ve bağlan'
                  : 'Kaydet ve yeniden bağlan',
            ),
          ),
          const SizedBox(height: 18),
          Card(
            color: const Color(0xFF111218),
            child: ListTile(
              leading: Icon(Icons.circle, size: 10, color: color),
              title: Text(status, style: const TextStyle(fontSize: 12)),
              trailing: c.hasCredentials
                  ? TextButton(
                      onPressed: c.reconnect,
                      child: const Text('Reconnect'),
                    )
                  : null,
            ),
          ),
          if (c.error != null)
            Padding(
              padding: const EdgeInsets.only(top: 10),
              child: Text(
                c.error!,
                style: const TextStyle(color: Color(0xFFDF8888), fontSize: 11),
              ),
            ),
          if (!widget.setupMode) ...[
            const SizedBox(height: 18),
            OutlinedButton.icon(
              onPressed: () async {
                await c.forgetToken();
                if (mounted) _token.clear();
              },
              icon: const Icon(Icons.logout),
              label: const Text('Bu cihazdaki tokenı unut'),
            ),
            const SizedBox(height: 10),
            const Text(
              'Sonraki aşama: Desktop QR / tek kullanımlık kod pairing.',
              style: TextStyle(fontSize: 9, color: Color(0xFF62656F)),
            ),
          ],
        ],
      ),
    );
  }
}

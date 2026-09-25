import 'package:flutter/material.dart';

import 'controller.dart';
import 'models.dart';
import 'screens/chat_screen.dart';
import 'screens/devices_screen.dart';
import 'screens/nightly_screen.dart';
import 'screens/settings_screen.dart';

class EpisApp extends StatefulWidget {
  const EpisApp({super.key, required this.controller});
  final EpisController controller;

  @override
  State<EpisApp> createState() => _EpisAppState();
}

class _EpisAppState extends State<EpisApp> {
  int _index = 0;

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      debugShowCheckedModeBanner: false,
      title: 'EPIS',
      theme: ThemeData(
        brightness: Brightness.dark,
        useMaterial3: true,
        scaffoldBackgroundColor: const Color(0xFF090A0E),
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF7565F6),
          brightness: Brightness.dark,
          surface: const Color(0xFF111218),
        ),
        appBarTheme: const AppBarTheme(
          backgroundColor: Color(0xFF0B0C11),
          surfaceTintColor: Colors.transparent,
        ),
        navigationBarTheme: const NavigationBarThemeData(
          backgroundColor: Color(0xFF0B0C11),
          indicatorColor: Color(0x337565F6),
        ),
      ),
      home: AnimatedBuilder(
        animation: widget.controller,
        builder: (context, _) {
          final c = widget.controller;
          if (!c.bootstrapped) {
            return const Scaffold(
              body: Center(child: CircularProgressIndicator()),
            );
          }
          if (!c.hasCredentials) {
            return SettingsScreen(controller: c, setupMode: true);
          }

          final pages = <Widget>[
            ChatScreen(controller: c),
            NightlyScreen(controller: c),
            DevicesScreen(controller: c),
            SettingsScreen(controller: c),
          ];

          return Scaffold(
            appBar: AppBar(
              titleSpacing: 16,
              title: const Row(
                children: [
                  CircleAvatar(
                    radius: 17,
                    backgroundColor: Color(0x227565F6),
                    child: Text('E'),
                  ),
                  SizedBox(width: 10),
                  Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'EPIS',
                        style: TextStyle(
                          fontSize: 14,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                      Text(
                        'Personal Intelligence System',
                        style: TextStyle(fontSize: 9, color: Color(0xFF6E717C)),
                      ),
                    ],
                  ),
                ],
              ),
              actions: [
                _ConnectionPill(
                  status: c.connection,
                  pcAgentOnline: c.pcAgentOnline,
                ),
                const SizedBox(width: 10),
              ],
            ),
            body: pages[_index],
            bottomNavigationBar: NavigationBar(
              selectedIndex: _index,
              onDestinationSelected: (value) {
                setState(() => _index = value);
                if (value == 2) c.refreshDevices();
              },
              destinations: const [
                NavigationDestination(
                  icon: Icon(Icons.chat_bubble_outline),
                  selectedIcon: Icon(Icons.chat_bubble),
                  label: 'Chat',
                ),
                NavigationDestination(
                  icon: Icon(Icons.auto_awesome_outlined),
                  selectedIcon: Icon(Icons.auto_awesome),
                  label: 'Nightly',
                ),
                NavigationDestination(
                  icon: Icon(Icons.devices_outlined),
                  selectedIcon: Icon(Icons.devices),
                  label: 'Devices',
                ),
                NavigationDestination(
                  icon: Icon(Icons.settings_outlined),
                  selectedIcon: Icon(Icons.settings),
                  label: 'Settings',
                ),
              ],
            ),
          );
        },
      ),
    );
  }
}

class _ConnectionPill extends StatelessWidget {
  const _ConnectionPill({required this.status, required this.pcAgentOnline});
  final EpisConnectionStatus status;
  final bool pcAgentOnline;

  @override
  Widget build(BuildContext context) {
    final (label, color) = switch (status) {
      EpisConnectionStatus.online => (
        pcAgentOnline ? 'PC ready' : 'Server',
        pcAgentOnline ? const Color(0xFF58D991) : const Color(0xFFD5AA55),
      ),
      EpisConnectionStatus.connecting => (
        'Bağlanıyor',
        const Color(0xFFD5AA55),
      ),
      EpisConnectionStatus.offline => ('Offline', const Color(0xFFDF6666)),
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 6),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: Colors.white.withValues(alpha: .07)),
      ),
      child: Row(
        children: [
          Container(
            width: 7,
            height: 7,
            decoration: BoxDecoration(color: color, shape: BoxShape.circle),
          ),
          const SizedBox(width: 6),
          Text(label, style: const TextStyle(fontSize: 10)),
        ],
      ),
    );
  }
}

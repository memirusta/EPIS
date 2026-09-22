import 'package:flutter/material.dart';

import '../controller.dart';

class DevicesScreen extends StatelessWidget {
  const DevicesScreen({super.key, required this.controller});
  final EpisController controller;

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: () async {
        controller.refreshDevices();
        await Future<void>.delayed(const Duration(milliseconds: 400));
      },
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Row(
            children: [
              const Expanded(
                child: Text(
                  'Devices',
                  style: TextStyle(fontSize: 28, fontWeight: FontWeight.w700),
                ),
              ),
              IconButton(
                onPressed: controller.refreshDevices,
                icon: const Icon(Icons.refresh),
              ),
            ],
          ),
          const SizedBox(height: 14),
          if (controller.devices.isEmpty)
            const Text(
              'Bağlı cihaz yok.',
              style: TextStyle(color: Color(0xFF777A85)),
            )
          else
            ...controller.devices.map(
              (device) => Card(
                color: const Color(0xFF111218),
                child: Padding(
                  padding: const EdgeInsets.all(15),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  device.displayName,
                                  style: const TextStyle(
                                    fontWeight: FontWeight.w700,
                                  ),
                                ),
                                Text(
                                  '${device.platform} · ${device.deviceId}',
                                  style: const TextStyle(
                                    fontSize: 9,
                                    color: Color(0xFF777A85),
                                  ),
                                ),
                              ],
                            ),
                          ),
                          Text(
                            device.online ? 'Online' : 'Offline',
                            style: TextStyle(
                              fontSize: 10,
                              color: device.online
                                  ? const Color(0xFF62D99A)
                                  : const Color(0xFFDF7777),
                            ),
                          ),
                        ],
                      ),
                      const SizedBox(height: 12),
                      Wrap(
                        spacing: 6,
                        runSpacing: 6,
                        children: device.capabilities
                            .map(
                              (cap) => Chip(
                                visualDensity: VisualDensity.compact,
                                label: Text(
                                  cap,
                                  style: const TextStyle(fontSize: 8),
                                ),
                              ),
                            )
                            .toList(growable: false),
                      ),
                    ],
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

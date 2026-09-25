import 'package:flutter/material.dart';

import '../controller.dart';
import '../models.dart';

class NightlyScreen extends StatelessWidget {
  const NightlyScreen({super.key, required this.controller});

  final EpisController controller;

  Color _statusColor(String status) => switch (status) {
    'success' || 'ok' || 'committed' || 'acked' => const Color(0xFF58D991),

    'running' || 'started' || 'pending' || 'warn' => const Color(0xFFD5AA55),

    'failed' || 'error' => const Color(0xFFDF6666),

    _ => const Color(0xFF81848F),
  };

  String _statusLabel(String status) => switch (status) {
    'success' => 'Complete',
    'running' => 'Running',
    'partial' => 'Partial',
    'failed' => 'Failed',
    'committed' => 'Committed',
    'acked' => 'Acked',
    'started' => 'Running',
    'ok' => 'OK',
    'pending' => 'Pending',
    'skip' => 'Skipped',
    'warn' => 'Warning',
    'error' => 'Error',
    _ => status,
  };

  @override
  Widget build(BuildContext context) {
    final run = controller.nightlyTrace;

    if (run == null) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.all(28),
          child: Text(
            'Henüz bir Nightly Recalculation izi yok. '
            'Sonraki NC başladığında bu ekran canlı güncellenecek.',
            textAlign: TextAlign.center,
            style: TextStyle(color: Color(0xFF7B7E89), height: 1.5),
          ),
        ),
      );
    }

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 18, 16, 28),
      children: [
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'Nightly Activity',
                    style: TextStyle(fontSize: 22, fontWeight: FontWeight.w700),
                  ),
                  SizedBox(height: 6),
                  Text(
                    'NC çalışma izi · raw transcript ve model düşünce zinciri gösterilmez.',
                    style: TextStyle(
                      color: Color(0xFF7B7E89),
                      fontSize: 11,
                      height: 1.45,
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(width: 12),
            _StatusChip(
              label: _statusLabel(run.status),
              color: _statusColor(run.status),
            ),
          ],
        ),
        const SizedBox(height: 18),
        Row(
          children: [
            Expanded(
              child: _MetaCard(label: 'Run', value: run.runId),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: _MetaCard(label: 'Day', value: run.dayId),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: _MetaCard(label: 'Events', value: '${run.events.length}'),
            ),
          ],
        ),
        const SizedBox(height: 18),
        for (final event in run.events)
          _TraceCard(
            event: event,
            statusLabel: _statusLabel(event.status ?? ''),
            statusColor: _statusColor(event.status ?? ''),
          ),
      ],
    );
  }
}

class _MetaCard extends StatelessWidget {
  const _MetaCard({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.all(12),
    decoration: BoxDecoration(
      color: Colors.white.withValues(alpha: .025),
      borderRadius: BorderRadius.circular(12),
      border: Border.all(color: Colors.white.withValues(alpha: .07)),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label.toUpperCase(),
          style: const TextStyle(fontSize: 8, color: Color(0xFF686B76)),
        ),
        const SizedBox(height: 5),
        Text(
          value,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: const TextStyle(fontSize: 10, fontWeight: FontWeight.w600),
        ),
      ],
    ),
  );
}

class _StatusChip extends StatelessWidget {
  const _StatusChip({required this.label, required this.color});

  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 6),
    decoration: BoxDecoration(
      borderRadius: BorderRadius.circular(999),
      border: Border.all(color: color.withValues(alpha: .28)),
    ),
    child: Text(
      label,
      style: TextStyle(fontSize: 9, color: color, fontWeight: FontWeight.w600),
    ),
  );
}

class _TraceCard extends StatelessWidget {
  const _TraceCard({
    required this.event,
    required this.statusLabel,
    required this.statusColor,
  });

  final NcTraceEvent event;
  final String statusLabel;
  final Color statusColor;

  @override
  Widget build(BuildContext context) => Container(
    margin: const EdgeInsets.only(bottom: 10),
    padding: const EdgeInsets.all(13),
    decoration: BoxDecoration(
      color: Colors.white.withValues(alpha: .025),
      borderRadius: BorderRadius.circular(12),
      border: Border.all(color: Colors.white.withValues(alpha: .07)),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              width: 8,
              height: 8,
              margin: const EdgeInsets.only(top: 4),
              decoration: BoxDecoration(
                color: statusColor,
                shape: BoxShape.circle,
              ),
            ),
            const SizedBox(width: 9),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    (event.stage ?? event.event).toUpperCase(),
                    style: const TextStyle(
                      color: Color(0xFF7565F6),
                      fontSize: 8,
                      fontWeight: FontWeight.w700,
                      letterSpacing: .7,
                    ),
                  ),
                  const SizedBox(height: 3),
                  Text(
                    event.title ?? event.event,
                    style: const TextStyle(
                      fontSize: 12,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ],
              ),
            ),
            _StatusChip(label: statusLabel, color: statusColor),
          ],
        ),
        if (event.detail != null && event.detail!.isNotEmpty) ...[
          const SizedBox(height: 9),
          Text(
            event.detail!,
            style: const TextStyle(
              fontSize: 10,
              height: 1.45,
              color: Color(0xFF858894),
            ),
          ),
        ],
        if (event.metrics.isNotEmpty) ...[
          const SizedBox(height: 10),
          Wrap(
            spacing: 7,
            runSpacing: 7,
            children: event.metrics.entries
                .map(
                  (entry) => Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 8,
                      vertical: 6,
                    ),
                    decoration: BoxDecoration(
                      color: Colors.white.withValues(alpha: .025),
                      borderRadius: BorderRadius.circular(8),
                      border: Border.all(
                        color: Colors.white.withValues(alpha: .05),
                      ),
                    ),
                    child: Text(
                      '${entry.key.replaceAll('_', ' ')}: ${entry.value}',
                      style: const TextStyle(
                        fontSize: 9,
                        color: Color(0xFFB2B3BB),
                      ),
                    ),
                  ),
                )
                .toList(growable: false),
          ),
        ],
      ],
    ),
  );
}

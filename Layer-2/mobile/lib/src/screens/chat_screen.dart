import 'package:flutter/material.dart';

import '../controller.dart';
import '../models.dart';

class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key, required this.controller});
  final EpisController controller;

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  final _text = TextEditingController();
  final _scroll = ScrollController();
  int _lastMessageCount = 0;
  String? _lastApprovalId;
  int? _approvalDurationMinutes;
  bool _nearBottom = true;

  @override
  void initState() {
    super.initState();
    _lastMessageCount = widget.controller.messages.length;
    _lastApprovalId = widget.controller.approval?.id;
    _scroll.addListener(_trackScrollPosition);
    widget.controller.addListener(_syncScroll);
  }

  void _trackScrollPosition() {
    if (!_scroll.hasClients) return;
    _nearBottom =
        (_scroll.position.maxScrollExtent - _scroll.position.pixels) < 120;
  }

  void _syncScroll() {
    final controller = widget.controller;
    final messageCount = controller.messages.length;
    final approvalId = controller.approval?.id;
    final newMessage = messageCount > _lastMessageCount;
    final newApproval = approvalId != null && approvalId != _lastApprovalId;
    if (newApproval) _approvalDurationMinutes = null;
    final userJustSent = newMessage &&
        controller.messages.isNotEmpty &&
        controller.messages.last.role == ChatRole.user;

    _lastMessageCount = messageCount;
    _lastApprovalId = approvalId;

    if (!(userJustSent || (_nearBottom && (newMessage || newApproval)))) {
      return;
    }

    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) {
        _scroll.animateTo(
          _scroll.position.maxScrollExtent,
          duration: const Duration(milliseconds: 180),
          curve: Curves.easeOut,
        );
      }
    });
  }

  void _send() {
    final value = _text.text.trim();
    if (value.isEmpty && widget.controller.pendingAttachments.isEmpty) return;
    if (widget.controller.sendMessage(value)) {
      _text.clear();
    }
  }

  String _attachmentSize(int bytes) {
    if (bytes < 1024) return '$bytes B';
    if (bytes < 1024 * 1024) return '${(bytes / 1024).toStringAsFixed(0)} KB';
    return '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
  }

  Widget _attachmentChip(
    ChatAttachmentSummary attachment, {
    VoidCallback? onDeleted,
  }) {
    final isImage = attachment.mimeType.toLowerCase().startsWith('image/');
    return InputChip(
      avatar: Icon(isImage ? Icons.image_outlined : Icons.description_outlined, size: 16),
      label: Text(
        '${attachment.name} · ${_attachmentSize(attachment.sizeBytes)}',
        overflow: TextOverflow.ellipsis,
      ),
      onDeleted: onDeleted,
      visualDensity: VisualDensity.compact,
    );
  }

  @override
  void dispose() {
    widget.controller.removeListener(_syncScroll);
    _scroll.removeListener(_trackScrollPosition);
    _text.dispose();
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final c = widget.controller;
    final online = c.connection == EpisConnectionStatus.online;
    return SafeArea(
      top: false,
      child: Column(
        children: [
          Expanded(
            child: c.messages.isEmpty
                ? const Center(
                    child: Text(
                      'Telefondan söyle, Core gerisini halletsin.',
                      style: TextStyle(color: Color(0xFF777A85)),
                    ),
                  )
                : ListView.builder(
                    controller: _scroll,
                    padding: const EdgeInsets.all(16),
                    itemCount: c.messages.length + (c.waiting ? 1 : 0),
                    itemBuilder: (context, index) {
                      if (index >= c.messages.length) {
                        return const Padding(
                          padding: EdgeInsets.symmetric(vertical: 10),
                          child: Row(
                            children: [
                              SizedBox(
                                width: 14,
                                height: 14,
                                child: CircularProgressIndicator(
                                  strokeWidth: 1.5,
                                ),
                              ),
                              SizedBox(width: 9),
                              Text(
                                'EPIS çalışıyor',
                                style: TextStyle(
                                  fontSize: 11,
                                  color: Color(0xFF777A85),
                                ),
                              ),
                            ],
                          ),
                        );
                      }
                      final message = c.messages[index];
                      final user = message.role == ChatRole.user;
                      return Align(
                        alignment: user
                            ? Alignment.centerRight
                            : Alignment.centerLeft,
                        child: Container(
                          constraints: const BoxConstraints(maxWidth: 520),
                          margin: const EdgeInsets.only(bottom: 12),
                          padding: const EdgeInsets.all(12),
                          decoration: BoxDecoration(
                            color: user
                                ? Colors.white.withValues(alpha: .055)
                                : Colors.transparent,
                            borderRadius: BorderRadius.circular(15),
                            border: user
                                ? Border.all(
                                    color: Colors.white.withValues(alpha: .07),
                                  )
                                : null,
                          ),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              if (message.text.isNotEmpty)
                                Text(
                                  message.text,
                                  style: const TextStyle(
                                    fontSize: 13,
                                    height: 1.45,
                                  ),
                                ),
                              if (message.attachments.isNotEmpty) ...[
                                const SizedBox(height: 8),
                                Wrap(
                                  spacing: 6,
                                  runSpacing: 6,
                                  children: message.attachments
                                      .map((attachment) => _attachmentChip(attachment))
                                      .toList(),
                                ),
                              ],
                              ...message.toolResults.map(
                                (tool) => ExpansionTile(
                                  dense: true,
                                  title: Text(
                                    (tool['status'] ??
                                            tool['capability'] ??
                                            'Araç işlemi')
                                        .toString(),
                                    style: const TextStyle(fontSize: 10),
                                  ),
                                  children: [
                                    Padding(
                                      padding: const EdgeInsets.all(8),
                                      child: SelectableText(
                                        tool.toString(),
                                        style: const TextStyle(
                                          fontSize: 9,
                                          color: Color(0xFF777A85),
                                        ),
                                      ),
                                    ),
                                  ],
                                ),
                              ),
                            ],
                          ),
                        ),
                      );
                    },
                  ),
          ),
          if (c.approval != null)
            Container(
              margin: const EdgeInsets.fromLTRB(12, 4, 12, 8),
              padding: const EdgeInsets.all(14),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(15),
                border: Border.all(color: const Color(0x557565F6)),
                color: const Color(0x187565F6),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                    'Onay gerekiyor',
                    style: TextStyle(fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(height: 5),
                  Text(
                    c.approval!.message,
                    style: const TextStyle(
                      fontSize: 11,
                      color: Color(0xFF9A9CA5),
                    ),
                  ),
                  if (c.approval!.whatsapp case final whatsapp?) ...[
                    const SizedBox(height: 8),
                    Text('Alıcı: ${whatsapp.contactName}'),
                    if (whatsapp.kind == 'auto_start' && whatsapp.goal != null)
                      Text('Amaç: ${whatsapp.goal}'),
                    if (whatsapp.maxAutoReplies != null)
                      Text('En fazla ${whatsapp.maxAutoReplies} otomatik cevap'),
                    const SizedBox(height: 4),
                    Text(whatsapp.message.isEmpty
                        ? 'İlk mesaj gönderilmeyecek.'
                        : 'Gönderilecek metin: ${whatsapp.message}'),
                    if (whatsapp.kind == 'auto_start')
                      DropdownButton<int?>(
                        value: _approvalDurationMinutes,
                        isExpanded: true,
                        items: const [
                          DropdownMenuItem<int?>(value: null, child: Text('Süresiz')),
                          DropdownMenuItem<int?>(value: 15, child: Text('15 dakika')),
                          DropdownMenuItem<int?>(value: 30, child: Text('30 dakika')),
                          DropdownMenuItem<int?>(value: 60, child: Text('1 saat')),
                          DropdownMenuItem<int?>(value: 120, child: Text('2 saat')),
                        ],
                        onChanged: (value) => setState(() {
                          _approvalDurationMinutes = value;
                        }),
                      ),
                  ],
                  Row(
                    mainAxisAlignment: MainAxisAlignment.end,
                    children: [
                      TextButton(
                        onPressed: c.rejectApproval,
                        child: const Text('Hayır'),
                      ),
                      FilledButton(
                        onPressed: () => c.confirmApproval(
                          durationMinutes: _approvalDurationMinutes,
                        ),
                        child: const Text('Evet'),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          if (c.error != null)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
              child: Text(
                c.error!,
                style: const TextStyle(color: Color(0xFFDF8888), fontSize: 11),
              ),
            ),
          if (c.pendingAttachments.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(12, 0, 12, 6),
              child: Align(
                alignment: Alignment.centerLeft,
                child: Wrap(
                  spacing: 6,
                  runSpacing: 6,
                  children: [
                    for (var i = 0; i < c.pendingAttachments.length; i++)
                      _attachmentChip(
                        c.pendingAttachments[i].summary,
                        onDeleted: () => c.removeAttachment(i),
                      ),
                  ],
                ),
              ),
            ),
          Padding(
            padding: EdgeInsets.fromLTRB(
              12,
              4,
              12,
              10 + MediaQuery.viewInsetsOf(context).bottom,
            ),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                IconButton(
                  tooltip: 'Dosya ekle',
                  onPressed: c.pickingAttachment ? null : c.pickAttachment,
                  icon: c.pickingAttachment
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 1.6),
                        )
                      : const Icon(Icons.attach_file),
                ),
                Expanded(
                  child: TextField(
                    controller: _text,
                    enabled: online,
                    minLines: 1,
                    maxLines: 5,
                    textInputAction: TextInputAction.send,
                    onSubmitted: (_) => _send(),
                    decoration: InputDecoration(
                      hintText: online
                          ? "EPIS'e bir şey söyle..."
                          : 'Server bağlantısı bekleniyor...',
                      filled: true,
                      fillColor: const Color(0xFF15161C),
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(16),
                        borderSide: BorderSide.none,
                      ),
                    ),
                  ),
                ),
                const SizedBox(width: 8),
                IconButton.filled(
                  onPressed: online ? _send : null,
                  icon: const Icon(Icons.arrow_upward),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

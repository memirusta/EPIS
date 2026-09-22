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

  @override
  void initState() {
    super.initState();
    widget.controller.addListener(_syncScroll);
  }

  void _syncScroll() {
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
    if (value.isEmpty) return;
    widget.controller.sendMessage(value);
    if (widget.controller.waiting) _text.clear();
  }

  @override
  void dispose() {
    widget.controller.removeListener(_syncScroll);
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
                  Row(
                    mainAxisAlignment: MainAxisAlignment.end,
                    children: [
                      TextButton(
                        onPressed: c.rejectApproval,
                        child: const Text('Hayır'),
                      ),
                      FilledButton(
                        onPressed: c.confirmApproval,
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
                  tooltip: 'Yeni bağlam',
                  onPressed: online && !c.waiting ? c.newConversation : null,
                  icon: const Icon(Icons.add_comment_outlined),
                ),
                Expanded(
                  child: TextField(
                    controller: _text,
                    enabled: online && !c.waiting,
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
                  onPressed: online && !c.waiting ? _send : null,
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

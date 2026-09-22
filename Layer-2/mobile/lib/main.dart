import 'package:flutter/material.dart';

import 'src/app.dart';
import 'src/controller.dart';
import 'src/secure_config.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final controller = EpisController(SecureConfigStore());
  await controller.bootstrap();
  runApp(EpisApp(controller: controller));
}

import 'package:flutter_test/flutter_test.dart';
import 'package:epis_mobile/src/models.dart';

void main() {
  test('approval parsing rejects incomplete payload', () {
    expect(ApprovalRequest.tryParse({'id': 'abc'}), isNull);
  });

  test('device parsing preserves capabilities', () {
    final device = DeviceSnapshot.tryParse({
      'device_id': 'desktop-test',
      'display_name': 'DESKTOP-TEST',
      'platform': 'windows',
      'online': true,
      'capabilities': ['apps.launch', 'computer.capture'],
    });
    expect(device, isNotNull);
    expect(device!.online, isTrue);
    expect(device.capabilities, contains('apps.launch'));
  });
}

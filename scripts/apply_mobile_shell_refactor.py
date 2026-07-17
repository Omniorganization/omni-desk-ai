#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def main() -> None:
    path = Path("apps/mobile-flutter/lib/main.dart")
    text = path.read_text(encoding="utf-8")
    text = text.replace("import 'package:flutter_secure_storage/flutter_secure_storage.dart';\n", "")
    text = text.replace("import 'package:firebase_messaging/firebase_messaging.dart';\n", "")
    text = text.replace("import 'package:local_auth/local_auth.dart';\n", "")
    text = text.replace(
        "import 'omni_api.dart';\nimport 'device_identity.dart';\n",
        "import 'omni_api.dart';\n"
        "import 'device_identity.dart';\n"
        "import 'app_models.dart';\n"
        "import 'mobile_security_service.dart';\n",
        1,
    )

    model_start = text.index("class ProjectItem {")
    app_main = text.index("Future<void> main() async {", model_start)
    text = text[:model_start] + text[app_main:]
    text = text.replace(
        "  static const _storage = FlutterSecureStorage();\n"
        "  final _auth = LocalAuthentication();\n",
        "  final securityService = MobileSecurityService();\n",
        1,
    )
    text = text.replace(
        "  DeviceIdentityStore get identityStore => DeviceIdentityStore(_storage);\n",
        "  DeviceIdentityStore get identityStore =>\n"
        "      DeviceIdentityStore(securityService.storage);\n",
        1,
    )

    restore_start = text.index("  Future<void> _restoreSession() async {")
    save_start = text.index("  Future<void> _saveSession() async {", restore_start)
    restore = """  Future<void> _restoreSession() async {
    final restored = await securityService.restoreSession(
      fallbackGateway: gatewayController.text,
      fallbackActor: actorController.text,
    );
    gatewayController.text = restored.gateway;
    tokenController.text = restored.token;
    actorController.text = restored.actor;
    if (mounted) setState(() {});
  }

"""
    text = text[:restore_start] + restore + text[save_start:]

    save_start = text.index("  Future<void> _saveSession() async {")
    operation_start = text.index("  String _operationKey(", save_start)
    save = """  Future<void> _saveSession() async {
    await securityService.saveSession(
      gateway: gatewayController.text,
      token: tokenController.text,
      actor: actorController.text,
    );
  }

"""
    text = text[:save_start] + save + text[operation_start:]

    confirm_start = text.index("  Future<bool> _confirmSensitiveAction() async {")
    push_start = text.index("  Future<String?> _resolvePushToken() async {", confirm_start)
    confirm = """  Future<bool> _confirmSensitiveAction() async {
    try {
      return await securityService.confirmSensitiveAction();
    } catch (_) {
      return false;
    }
  }

"""
    text = text[:confirm_start] + confirm + text[push_start:]

    push_start = text.index("  Future<String?> _resolvePushToken() async {")
    active_project_start = text.index("  ProjectItem? get activeProject {", push_start)
    push = """  Future<String?> _resolvePushToken() async {
    try {
      return await securityService.resolvePushToken();
    } catch (_) {
      return null;
    }
  }

"""
    text = text[:push_start] + push + text[active_project_start:]
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()

class OmniMobileConfig {
  static const defaultGatewayUrl =
      String.fromEnvironment('OMNI_MOBILE_GATEWAY_URL', defaultValue: '');

  static String? validateGatewayUrl(String value) {
    final trimmed = value.trim();
    if (trimmed.isEmpty) {
      return 'Gateway URL is required. Use a LAN/VPN URL reachable from this phone.';
    }

    final uri = Uri.tryParse(trimmed);
    if (uri == null || !uri.hasScheme || uri.host.isEmpty) {
      return 'Gateway URL must be an absolute http or https URL.';
    }
    if (uri.scheme != 'http' && uri.scheme != 'https') {
      return 'Gateway URL must use http or https.';
    }
    if (_isLoopbackHost(uri.host)) {
      return 'Gateway URL must be reachable from this phone; do not use localhost or 127.0.0.1.';
    }
    return null;
  }

  static bool _isLoopbackHost(String host) {
    final normalized = host.toLowerCase();
    return normalized == 'localhost' ||
        normalized == '::1' ||
        normalized == '0:0:0:0:0:0:0:1' ||
        normalized == '0.0.0.0' ||
        normalized.startsWith('127.');
  }
}

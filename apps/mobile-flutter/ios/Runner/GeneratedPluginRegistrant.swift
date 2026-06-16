import Flutter
import UIKit

import firebase_core
import firebase_messaging
import flutter_secure_storage
import local_auth_darwin
import path_provider_foundation

// Generated from the locked Flutter plugin set so release CI does not ship the source placeholder.
final class GeneratedPluginRegistrant {
  static func register(with registry: FlutterPluginRegistry) {
    FLTFirebaseCorePlugin.register(with: registry.registrar(forPlugin: "FLTFirebaseCorePlugin"))
    FLTFirebaseMessagingPlugin.register(with: registry.registrar(forPlugin: "FLTFirebaseMessagingPlugin"))
    FlutterSecureStoragePlugin.register(with: registry.registrar(forPlugin: "FlutterSecureStoragePlugin"))
    LocalAuthPlugin.register(with: registry.registrar(forPlugin: "LocalAuthPlugin"))
    PathProviderPlugin.register(with: registry.registrar(forPlugin: "PathProviderPlugin"))
  }
}

// PM-WANI backend client for the Flutter app.
// Add to pubspec.yaml:  http: ^1.2.2   shared_preferences: ^2.3.2
// Usage:
//   final api = PmwaniApi(baseUrl: 'http://3.7.159.119/api/v1');
//   await api.loadToken();
//   final req = await api.requestOtp('9876543210');      // req['demo_otp'] is set in demo mode
//   final login = await api.verifyOtp(req['request_id'], '123456');
//   final plans = await api.plans();
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

class ApiException implements Exception {
  final int status;
  final String code; // e.g. no_active_plan, insufficient_balance, otp_invalid
  ApiException(this.status, this.code);
  @override
  String toString() => 'ApiException($status, $code)';
}

class PmwaniApi {
  PmwaniApi({required this.baseUrl, http.Client? client}) : _http = client ?? http.Client();

  final String baseUrl;
  final http.Client _http;
  String? _token;
  static const _tokenKey = 'pmwani_token';

  bool get isLoggedIn => _token != null;

  Future<void> loadToken() async {
    _token = (await SharedPreferences.getInstance()).getString(_tokenKey);
  }

  Future<void> logout() async {
    _token = null;
    (await SharedPreferences.getInstance()).remove(_tokenKey);
  }

  // ------------------------------------------------------------------ core
  Future<dynamic> _send(String method, String path, {Object? body, Map<String, String>? query}) async {
    final uri = Uri.parse('$baseUrl$path').replace(queryParameters: query);
    final headers = {
      'Content-Type': 'application/json',
      if (_token != null) 'Authorization': 'Bearer $_token',
    };
    final req = http.Request(method, uri)..headers.addAll(headers);
    if (body != null) req.body = jsonEncode(body);
    final res = await http.Response.fromStream(await _http.send(req).timeout(const Duration(seconds: 20)));
    final data = res.body.isEmpty ? null : jsonDecode(utf8.decode(res.bodyBytes));
    if (res.statusCode >= 400) {
      final detail = data is Map ? data['detail'] : null;
      throw ApiException(res.statusCode, detail is String ? detail : 'error_${res.statusCode}');
    }
    return data;
  }

  Future<dynamic> _get(String p, [Map<String, String>? q]) => _send('GET', p, query: q);
  Future<dynamic> _post(String p, [Object? b]) => _send('POST', p, body: b ?? {});

  // ------------------------------------------------------------------ auth
  Future<Map<String, dynamic>> requestOtp(String phone) async =>
      Map<String, dynamic>.from(await _post('/auth/otp/request', {'phone': phone}));

  /// Returns {access_token, user}. The token is saved automatically.
  Future<Map<String, dynamic>> verifyOtp(String requestId, String otp) async {
    final r = Map<String, dynamic>.from(await _post('/auth/otp/verify', {'request_id': requestId, 'otp': otp}));
    _token = r['access_token'] as String;
    (await SharedPreferences.getInstance()).setString(_tokenKey, _token!);
    return r;
  }

  Future<Map<String, dynamic>> me() async => Map<String, dynamic>.from(await _get('/me'));
  Future<dynamic> updateProfile({String? name, String? email}) =>
      _send('PATCH', '/me', body: {'name': name, 'email': email});
  Future<dynamic> submitKyc(String docType, String docNumber, String nameOnDoc) =>
      _post('/me/kyc', {'doc_type': docType, 'doc_number': docNumber, 'name_on_doc': nameOnDoc});
  Future<dynamic> applyAsPdo(String businessName, String address) =>
      _post('/me/pdo-application', {'business_name': businessName, 'address': address});

  // -------------------------------------------------------- consumer data
  Future<List<dynamic>> hotspots({double? lat, double? lng, double radiusKm = 25}) async => await _get('/hotspots', {
        if (lat != null) 'lat': '$lat',
        if (lng != null) 'lng': '$lng',
        'radius_km': '$radiusKm',
      });
  Future<List<dynamic>> plans() async => await _get('/plans');
  Future<List<dynamic>> subscriptions() async => await _get('/subscriptions');
  Future<List<dynamic>> sessions({int limit = 50}) async => await _get('/sessions', {'limit': '$limit'});
  Future<Map<String, dynamic>> wifiStatus() async => Map<String, dynamic>.from(await _get('/wifi/status'));

  /// method: 'free' | 'wallet' | 'gateway'. 'gateway' returns {order: {...}} -> open Razorpay Checkout.
  Future<Map<String, dynamic>> buyPlan(int planId, {String method = 'wallet'}) async =>
      Map<String, dynamic>.from(await _post('/plans/$planId/buy', {'method': method}));

  // -------------------------------------------------------- wallet/payment
  Future<Map<String, dynamic>> wallet() async => Map<String, dynamic>.from(await _get('/wallet'));

  /// Returns an order. If order['gateway'] == 'razorpay', open Razorpay Checkout with
  /// key=order['razorpay_key_id'], order_id=order['gateway_order_id'], amount=order['amount_paise'];
  /// then call verifyPayment(). In 'mock' mode call verifyPayment(orderId, 'mock_pay', 'mock').
  Future<Map<String, dynamic>> topUp(int amountPaise) async =>
      Map<String, dynamic>.from(await _post('/wallet/topup', {'amount_paise': amountPaise}));

  Future<Map<String, dynamic>> verifyPayment(int orderId, String paymentId, String signature) async =>
      Map<String, dynamic>.from(await _post('/payments/verify', {
        'order_id': orderId,
        'razorpay_payment_id': paymentId,
        'razorpay_signature': signature,
      }));

  // ------------------------------------------------------------ Wi-Fi login
  /// [redirectUrl] is the URL CoovaChilli redirected to, e.g.
  /// http://3.7.159.119/chilli?res=notyet&uamip=10.1.0.1&uamport=3990&challenge=...&mac=...&nasid=...
  /// Returns {logon_url, session_timeout, remaining_bytes, plan}. Open logon_url OVER THE WI-FI NETWORK.
  /// Throws ApiException(402, 'no_active_plan') if the user must buy/claim a plan first.
  Future<Map<String, dynamic>> wifiLogin(Uri redirectUrl) async {
    final q = redirectUrl.queryParameters;
    return Map<String, dynamic>.from(await _post('/wifi/login', {
      'uamip': q['uamip'],
      'uamport': q['uamport'],
      'challenge': q['challenge'],
      'mac': q['mac'],
      'nasid': q['nasid'],
      'userurl': q['userurl'],
    }));
  }

  // ------------------------------------------------------------------ PDO
  Future<List<dynamic>> pdoAccessPoints() async => await _get('/pdo/access-points');
  Future<Map<String, dynamic>> pdoAddAccessPoint(
          {required String name, String ssid = 'PMWANI-Free', String? mac, double? lat, double? lng, String? address}) async =>
      Map<String, dynamic>.from(await _post('/pdo/access-points',
          {'name': name, 'ssid': ssid, 'mac': mac, 'lat': lat, 'lng': lng, 'address': address}));
  Future<Map<String, dynamic>> pdoStats({int days = 30}) async =>
      Map<String, dynamic>.from(await _get('/pdo/stats', {'days': '$days'}));

  // ----------------------------------------------------------------- PDOA
  Future<Map<String, dynamic>> pdoaDashboard() async => Map<String, dynamic>.from(await _get('/pdoa/dashboard'));
  Future<List<dynamic>> pdoaUsers({String? role, String? pdoStatus, String? phone}) async => await _get('/pdoa/users', {
        if (role != null) 'role': role,
        if (pdoStatus != null) 'pdo_status': pdoStatus,
        if (phone != null) 'phone': phone,
      });
  Future<dynamic> pdoaDecidePdo(int userId, bool approve) =>
      _post('/pdoa/pdo-applications/$userId/decision', {'approve': approve});
  Future<dynamic> pdoaDecideKyc(int userId, bool approve) => _post('/pdoa/users/$userId/kyc', {'approve': approve});
  Future<List<dynamic>> pdoaAccessPoints({String? status}) async =>
      await _get('/pdoa/access-points', {if (status != null) 'status': status});
  Future<dynamic> pdoaSetApStatus(int apId, String status) =>
      _post('/pdoa/access-points/$apId/status', {'status': status});
  Future<List<dynamic>> pdoaPlans() async => await _get('/pdoa/plans');
  Future<dynamic> pdoaCreatePlan(Map<String, dynamic> plan) => _post('/pdoa/plans', plan);
  Future<dynamic> pdoaUpdatePlan(int id, Map<String, dynamic> plan) => _send('PUT', '/pdoa/plans/$id', body: plan);
  Future<Map<String, dynamic>> pdoaRegistryExport() async =>
      Map<String, dynamic>.from(await _get('/pdoa/registry/export'));
}

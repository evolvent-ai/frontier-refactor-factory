"""Public test material verified byte-for-byte against immutable upstream sources.

These are content-specific classifications, never directory or organization exemptions.
Configured production secrets and internal-identity hits cannot use these classifications.
"""
GO_SOURCE = 'https://raw.githubusercontent.com/golang/go/d90b98e65320778f3b1f99a6951ab20f04d218b3/'

PUBLIC_MATERIAL = (
    {'sha256': '4986dd7d7c0d9048453c724769d3ba5305a2cc4f57e9adb22b12b800c501f37b',
     'source': 'https://hub.docker.com/layers/library/python/3.12.8-slim-bookworm/images/'
               'sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0',
     'source_image_digest': 'sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0',
     'source_member': 'usr/lib/x86_64-linux-gnu/libgnutls.so.30.34.3',
     'source_package': 'libgnutls30 3.7.9-2+deb12u3 amd64',
     'reason': 'Public Debian GnuTLS library shipped in the immutable official Python base image'},
    {'sha256': '750be5c0ce95061563e1561019e382823eb1e011ffe7980e9e005eeafd82efa3',
     'source': 'https://hub.docker.com/layers/library/rust/1.90-bookworm/images/'
               'sha256:3914072ca0c3b8aad871db9169a651ccfce30cf58303e5d6f2db16d1d8a7e58f',
     'source_image_digest': 'sha256:3914072ca0c3b8aad871db9169a651ccfce30cf58303e5d6f2db16d1d8a7e58f',
     'source_member': 'usr/lib/x86_64-linux-gnu/libgnutls.so.30.34.3',
     'source_package': 'libgnutls30 3.7.9-2+deb12u5 amd64',
     'reason': 'Public Debian GnuTLS library shipped in the immutable official Rust base image'},
    {'sha256': '779b25d20249988bea2c1aa6bbeb218f5ae7ea8a9d30ce4f54ea37372965cc4b',
     'source': 'https://deb.debian.org/debian-security/pool/updates/main/g/gnutls28/'
               'libgnutls30_3.7.9-2+deb12u7_amd64.deb',
     'source_archive_sha256': '30abec8c824feb1d2d7e9000a34083cccd19d139625e1b21547e3ac53b922f8e',
     'source_member': 'usr/lib/x86_64-linux-gnu/libgnutls.so.30.34.3',
     'reason': 'Public Debian GnuTLS library containing built-in key material'},
    {'sha256': '0c1c065aea98125a16fe59fe30df13e57400179fa5b3c2503fb3b98dcc99457d',
     'source': GO_SOURCE + 'src/crypto/tls/example_test.go',
     'reason': 'Go upstream public TLS example containing test keys'},
    {'sha256': '4280dcaf02ddf203cc1e3179b872f2e2f130662437b2f525bb24dfab4e7fade3',
     'source': GO_SOURCE + 'src/crypto/tls/testdata/example-key.pem',
     'reason': 'Go upstream public TLS fixture key'},
    {'sha256': '2fea2ebe77388df61566fcd43eb0070dbff2842c8786a0e7906a13e5f10c3e65',
     'source': GO_SOURCE + 'src/crypto/x509/platform_root_key.pem',
     'reason': 'Go upstream public X509 fixture key'},
)

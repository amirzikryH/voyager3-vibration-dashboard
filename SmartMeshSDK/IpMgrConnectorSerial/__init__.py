# Ported to Python 3: expose the pure-Python connector module (skip the C impl).
# Usage stays identical to the original SDK:
#     from SmartMeshSDK.IpMgrConnectorSerial import IpMgrConnectorSerial
#     conn = IpMgrConnectorSerial.IpMgrConnectorSerial()
from . import IpMgrConnectorSerial

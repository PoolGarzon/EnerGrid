"""EnerGrid - reglas de negocio y catálogos compartidos por el generador y el ETL (única fuente de verdad)."""

REGIONS = ["Norte", "Centro", "Sur", "Valle"]
CLIENT_TYPES = ["Residencial", "Comercial", "Industrial"]
STATUSES = ["Leído", "Estimado", "Faltante"]
PAY_METHODS = ["Transferencia", "Efectivo"]

READING_DAY = 25      # día del mes en que se cierra la lectura
INVOICE_DAY = 28      # día del mes en que se emite la factura
DUE_DAYS = 20         # días de plazo desde la emisión hasta el vencimiento
FIXED_CHARGE = 1.5    # cargo fijo por factura (USD)

# Tarifa que corresponde al tipo de cliente (Social = 4 solo se asigna a residenciales con subsidio)
TARIFF_BY_CLIENT_TYPE = {"Residencial": 1, "Comercial": 2, "Industrial": 3}

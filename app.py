import os
from datetime import date, datetime
from functools import wraps
from flask import Flask, jsonify, render_template, request, session, redirect, url_for
from supabase import create_client, Client
from dotenv import load_dotenv
from werkzeug.security import check_password_hash

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambiar-en-produccion")

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")

if not url or not key:
    print("⚠️ Faltan credenciales de Supabase")
    supabase = None
else:
    supabase: Client = create_client(url, key)
    print("✅ Conectado a Supabase")


# ---------------- DECORADORES ----------------

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get('rol') != 'admin':
            return jsonify({'error': 'No autorizado'}), 403
        return f(*args, **kwargs)
    return wrapper


def auditoria(accion, detalle=None):
    try:
        supabase.table('auditoria').insert({
            'usuario': session.get('username', '?'),
            'accion': accion,
            'detalle': detalle or {}
        }).execute()
    except Exception as e:
        print(f"Error auditoría: {e}")


# ---------------- LOGIN ----------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('login.html')

    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return jsonify({'error': 'Usuario y contraseña requeridos'}), 400

    try:
        res = supabase.table('usuarios').select('*').eq('username', username).eq('activo', True).execute()
        if not res.data:
            return jsonify({'error': 'Usuario no encontrado'}), 401

        user = res.data[0]
        if not check_password_hash(user['password_hash'], password):
            return jsonify({'error': 'Contraseña incorrecta'}), 401

        session['username'] = user['username']
        session['rol'] = user['rol']
        session['nombre'] = user.get('nombre', user['username'])
        session['tienda_id'] = user.get('tienda_id')

        if user.get('tienda_id'):
            tienda = supabase.table('tiendas').select('nombre').eq('id', user['tienda_id']).execute()
            if tienda.data:
                session['tienda_nombre'] = tienda.data[0]['nombre']
            else:
                session['tienda_nombre'] = 'Sin tienda'
        else:
            session['tienda_nombre'] = 'Todas'

        auditoria('login')
        return jsonify({'mensaje': 'Bienvenido', 'rol': user['rol']}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/logout')
def logout():
    auditoria('logout')
    session.clear()
    return redirect(url_for('login'))


# ---------------- VISTAS ----------------

@app.route('/')
@login_required
def index():
    if session.get('rol') == 'admin':
        return render_template('dashboard.html')
    return redirect(url_for('ventas'))


@app.route('/ventas')
@login_required
def ventas():
    return render_template('ventas.html')


@app.route('/caja')
@login_required
def caja():
    return render_template('caja.html')


@app.route('/admin/verificar')
@login_required
@admin_required
def admin_verificar():
    return render_template('admin_verificar.html')


@app.route('/inventario')
@login_required
@admin_required
def inventario():
    return render_template('inventario.html')


# ---------------- API: MIS DATOS ----------------

@app.route('/api/mis-datos')
@login_required
def mis_datos():
    return jsonify({
        'username': session.get('username'),
        'rol': session.get('rol'),
        'tienda_id': session.get('tienda_id'),
        'tienda_nombre': session.get('tienda_nombre')
    })


# ---------------- API: PRODUCTOS ----------------

@app.route('/api/productos')
@login_required
def get_productos():
    try:
        tienda_id = session.get('tienda_id')
        if not tienda_id:
            return jsonify({'error': 'Usuario sin tienda asignada'}), 400

        response = supabase.table('productos_con_stock').select('*').eq('tienda_id', tienda_id).order('nombre').execute()
        return jsonify(response.data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------- API: VENTAS ----------------

@app.route('/api/venta', methods=['POST'])
@login_required
def registrar_venta():
    data = request.get_json()
    items = data.get('items', [])
    metodo_pago = data.get('metodo_pago', 'efectivo')

    if not items:
        return jsonify({'error': 'Carrito vacío'}), 400

    total = sum(item['precio'] * item['cantidad'] for item in items)
    usuario = session.get('username')
    tienda_id = session.get('tienda_id')

    try:
        venta_res = supabase.table('ventas').insert({
            'total': total,
            'metodo_pago': metodo_pago,
            'estado': 'completada',
            'vendedor': usuario,
            'usuario': usuario,
            'tienda_id': tienda_id
        }).execute()

        venta_id = venta_res.data[0]['id']

        for item in items:
            supabase.table('venta_detalle').insert({
                'venta_id': venta_id,
                'producto_id': item['id'],
                'cantidad': item['cantidad'],
                'precio_unitario': item['precio']
            }).execute()

            stock_res = supabase.table('stock_tiendas').select('stock').eq('producto_id', item['id']).eq('tienda_id', tienda_id).execute()
            if not stock_res.data:
                return jsonify({'error': 'Producto sin stock en esta tienda'}), 400

            stock_actual = stock_res.data[0]['stock']
            nuevo_stock = stock_actual - item['cantidad']

            supabase.table('stock_tiendas').update({'stock': nuevo_stock}).eq('producto_id', item['id']).eq('tienda_id', tienda_id).execute()

            supabase.table('movimientos_inventario').insert({
                'producto_id': item['id'],
                'tienda_id': tienda_id,
                'tipo': 'venta',
                'cantidad': item['cantidad'],
                'referencia_id': venta_id
            }).execute()

        auditoria('venta', {'venta_id': venta_id, 'total': total, 'tienda': tienda_id})
        return jsonify({'mensaje': 'Venta registrada', 'venta_id': venta_id, 'total': total}), 201

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------- API: CAJA ----------------

@app.route('/api/caja/abrir', methods=['POST'])
@login_required
def abrir_caja():
    data = request.get_json()
    efectivo_inicial = float(data.get('efectivo_inicial', 0))
    hoy = date.today().isoformat()
    tienda_id = session.get('tienda_id')

    existente = supabase.table('caja_sesiones').select('*').eq('fecha', hoy).eq('tienda_id', tienda_id).execute()
    if existente.data:
        return jsonify({'error': 'Ya hay una caja abierta hoy en esta tienda'}), 400

    try:
        res = supabase.table('caja_sesiones').insert({
            'fecha': hoy,
            'efectivo_inicial': efectivo_inicial,
            'abierto_por': session.get('username'),
            'tienda_id': tienda_id,
            'estado_cierre': 'abierta'
        }).execute()
        auditoria('caja_abierta', {'efectivo_inicial': efectivo_inicial, 'tienda': tienda_id})
        return jsonify({'mensaje': 'Caja abierta', 'caja': res.data[0]}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/caja/estado')
@login_required
def estado_caja():
    hoy = date.today().isoformat()
    tienda_id = session.get('tienda_id')

    try:
        caja = supabase.table('caja_sesiones').select('*').eq('fecha', hoy).eq('tienda_id', tienda_id).execute()
        if not caja.data:
            return jsonify({'abierta': False}), 200

        caja_data = caja.data[0]
        es_admin = session.get('rol') == 'admin'

        ventas = supabase.table('ventas').select('total, metodo_pago').eq('estado', 'completada').eq('tienda_id', tienda_id).gte('fecha', hoy + 'T00:00:00').execute()
        ventas_efectivo = sum(v['total'] for v in ventas.data if v['metodo_pago'] == 'efectivo')
        ventas_yape = sum(v['total'] for v in ventas.data if v['metodo_pago'] in ('yape', 'plin'))

        efectivo_inicial = float(caja_data.get('efectivo_inicial') or 0)
        gastos = float(caja_data.get('gastos') or 0)
        efectivo_esperado = efectivo_inicial + ventas_efectivo - gastos

        respuesta = {
            'abierta': True,
            'efectivo_inicial': efectivo_inicial,
            'gastos': gastos,
            'estado_cierre': caja_data.get('estado_cierre', 'abierta'),
            'conteo_registrado': caja_data.get('conteo_registrado', False),
            'cerrado_en': caja_data.get('cerrado_en')
        }

        if es_admin:
            respuesta.update({
                'ventas_efectivo': ventas_efectivo,
                'ventas_yape': ventas_yape,
                'total_ventas': ventas_efectivo + ventas_yape,
                'efectivo_esperado': efectivo_esperado,
                'efectivo_contado': caja_data.get('efectivo_contado'),
                'diferencia': caja_data.get('diferencia')
            })

        return jsonify(respuesta), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/caja/gasto', methods=['POST'])
@login_required
def registrar_gasto():
    data = request.get_json()
    monto = float(data.get('monto', 0))
    motivo = data.get('motivo', '')

    if monto <= 0:
        return jsonify({'error': 'Monto inválido'}), 400

    hoy = date.today().isoformat()
    tienda_id = session.get('tienda_id')
    caja = supabase.table('caja_sesiones').select('*').eq('fecha', hoy).eq('tienda_id', tienda_id).execute()
    if not caja.data:
        return jsonify({'error': 'No hay caja abierta'}), 400

    caja_data = caja.data[0]
    gastos_actuales = float(caja_data.get('gastos') or 0)
    nuevos_gastos = gastos_actuales + monto

    try:
        supabase.table('caja_sesiones').update({'gastos': nuevos_gastos}).eq('id', caja_data['id']).execute()
        auditoria('gasto', {'monto': monto, 'motivo': motivo, 'tienda': tienda_id})
        return jsonify({'mensaje': 'Gasto registrado', 'gastos_totales': nuevos_gastos}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/caja/registrar-conteo', methods=['POST'])
@login_required
def registrar_conteo():
    data = request.get_json()
    efectivo_contado = float(data.get('efectivo_contado', 0))
    observaciones = data.get('observaciones', '')

    hoy = date.today().isoformat()
    tienda_id = session.get('tienda_id')
    caja = supabase.table('caja_sesiones').select('*').eq('fecha', hoy).eq('tienda_id', tienda_id).execute()
    if not caja.data:
        return jsonify({'error': 'No hay caja abierta'}), 400

    caja_data = caja.data[0]

    if caja_data.get('conteo_registrado'):
        return jsonify({'error': 'Ya registraste tu conteo hoy'}), 400

    try:
        ventas = supabase.table('ventas').select('total, metodo_pago').eq('estado', 'completada').eq('tienda_id', tienda_id).gte('fecha', hoy + 'T00:00:00').execute()
        ventas_efectivo = sum(v['total'] for v in ventas.data if v['metodo_pago'] == 'efectivo')

        efectivo_inicial = float(caja_data.get('efectivo_inicial') or 0)
        gastos = float(caja_data.get('gastos') or 0)
        efectivo_esperado = efectivo_inicial + ventas_efectivo - gastos
        diferencia_vendedora = efectivo_contado - efectivo_esperado

        supabase.table('caja_sesiones').update({
            'ventas_efectivo': ventas_efectivo,
            'efectivo_esperado': efectivo_esperado,
            'conteo_vendedora': efectivo_contado,
            'diferencia_vendedora': diferencia_vendedora,
            'conteo_registrado': True,
            'cerrado_por': session.get('username'),
            'cerrado_en': datetime.now().isoformat(),
            'estado_cierre': 'pendiente_verificacion'
        }).eq('id', caja_data['id']).execute()

        auditoria('conteo_vendedora', {
            'contado': efectivo_contado,
            'diferencia': diferencia_vendedora,
            'tienda': tienda_id
        })

        return jsonify({
            'mensaje': 'Conteo registrado. En revisión por el administrador.',
            'recibido': True
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/caja/reabrir', methods=['POST'])
@login_required
@admin_required
def reabrir_caja():
    """Solo el admin puede reabrir una caja ya cerrada hoy"""
    data = request.get_json()
    tienda_id = int(data.get('tienda_id', 0))

    if tienda_id not in (1, 2):
        return jsonify({'error': 'Tienda inválida'}), 400

    hoy = date.today().isoformat()

    try:
        caja = supabase.table('caja_sesiones').select('*').eq('fecha', hoy).eq('tienda_id', tienda_id).execute()
        if not caja.data:
            return jsonify({'error': 'No hay caja hoy en esa tienda'}), 404

        caja_data = caja.data[0]

        if not caja_data.get('conteo_registrado'):
            return jsonify({'error': 'Esta caja no ha sido cerrada aún'}), 400

        supabase.table('caja_sesiones').update({
            'conteo_registrado': False,
            'estado_cierre': 'abierta',
            'conteo_vendedora': None,
            'conteo_admin': None,
            'diferencia_vendedora': None,
            'diferencia_admin': None,
            'cerrado_por': None,
            'cerrado_en': None,
            'revisado_por_admin': False
        }).eq('id', caja_data['id']).execute()

        auditoria('caja_reabierta', {'caja_id': caja_data['id'], 'tienda': tienda_id})
        return jsonify({'mensaje': 'Caja reabierta correctamente'}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------- API: VERIFICACIÓN ADMIN ----------------

@app.route('/api/admin/cierres-pendientes')
@login_required
@admin_required
def cierres_pendientes():
    try:
        res = supabase.table('caja_sesiones').select(
            '*, tiendas(nombre)'
        ).eq('estado_cierre', 'pendiente_verificacion').order('fecha', desc=True).execute()
        return jsonify(res.data), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/cierre/<int:caja_id>')
@login_required
@admin_required
def detalle_cierre(caja_id):
    try:
        res = supabase.table('caja_sesiones').select('*, tiendas(nombre)').eq('id', caja_id).execute()
        if not res.data:
            return jsonify({'error': 'Cierre no encontrado'}), 404
        return jsonify(res.data[0]), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/verificar-cierre/<int:caja_id>', methods=['POST'])
@login_required
@admin_required
def verificar_cierre(caja_id):
    data = request.get_json()
    conteo_admin = float(data.get('conteo_admin', 0))
    observaciones_admin = data.get('observaciones', '')

    try:
        caja = supabase.table('caja_sesiones').select('*').eq('id', caja_id).execute()
        if not caja.data:
            return jsonify({'error': 'Cierre no encontrado'}), 404

        caja_data = caja.data[0]
        efectivo_esperado = float(caja_data.get('efectivo_esperado') or 0)
        conteo_vendedora = float(caja_data.get('conteo_vendedora') or 0)

        diferencia_admin = conteo_admin - efectivo_esperado
        diferencia_entre_conteos = conteo_admin - conteo_vendedora

        if abs(diferencia_admin) < 0.01 and abs(diferencia_entre_conteos) < 0.01:
            estado = 'verificada'
        else:
            estado = 'con_diferencia'

        supabase.table('caja_sesiones').update({
            'conteo_admin': conteo_admin,
            'diferencia_admin': diferencia_admin,
            'revisado_por_admin': True,
            'estado_cierre': estado
        }).eq('id', caja_id).execute()

        auditoria('verificacion_admin', {
            'caja_id': caja_id,
            'conteo_admin': conteo_admin,
            'diferencia_admin': diferencia_admin,
            'diferencia_entre_conteos': diferencia_entre_conteos,
            'estado': estado,
            'observaciones': observaciones_admin
        })

        return jsonify({
            'mensaje': 'Cierre verificado',
            'conteo_vendedora': conteo_vendedora,
            'conteo_admin': conteo_admin,
            'efectivo_esperado': efectivo_esperado,
            'diferencia_admin': diferencia_admin,
            'diferencia_entre_conteos': diferencia_entre_conteos,
            'estado': estado
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------- API: INVENTARIO (ADMIN) ----------------

@app.route('/api/admin/inventario')
@login_required
@admin_required
def get_inventario():
    try:
        productos = supabase.table('productos').select('*').order('nombre').execute()
        stocks = supabase.table('stock_tiendas').select('*').execute()

        mapa = {}
        for s in stocks.data:
            mapa.setdefault(s['producto_id'], {})[s['tienda_id']] = {
                'stock': s['stock'],
                'stock_minimo': s.get('stock_minimo', 5),
                'id': s['id']
            }

        resultado = []
        for p in productos.data:
            resultado.append({
                'id': p['id'],
                'codigo': p.get('codigo') or '',
                'nombre': p['nombre'],
                'categoria': p.get('categoria') or '',
                'precio': float(p['precio']),
                't1': mapa.get(p['id'], {}).get(1, {'stock': 0, 'stock_minimo': 5}),
                't2': mapa.get(p['id'], {}).get(2, {'stock': 0, 'stock_minimo': 5})
            })
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/producto', methods=['POST'])
@login_required
@admin_required
def crear_producto():
    data = request.get_json()
    codigo = (data.get('codigo') or '').strip() or None
    nombre = (data.get('nombre') or '').strip()
    categoria = (data.get('categoria') or '').strip()
    precio = float(data.get('precio', 0))
    stock_t1 = int(data.get('stock_t1', 0))
    stock_t2 = int(data.get('stock_t2', 0))
    minimo_t1 = int(data.get('minimo_t1', 5))
    minimo_t2 = int(data.get('minimo_t2', 5))

    if not nombre:
        return jsonify({'error': 'El nombre es obligatorio'}), 400
    if precio <= 0:
        return jsonify({'error': 'El precio debe ser mayor a 0'}), 400

    try:
        if codigo:
            existe = supabase.table('productos').select('id').eq('codigo', codigo).execute()
            if existe.data:
                return jsonify({'error': 'Ya existe un producto con ese código'}), 400

        prod = supabase.table('productos').insert({
            'codigo': codigo,
            'nombre': nombre,
            'categoria': categoria or None,
            'precio': precio
        }).execute()
        prod_id = prod.data[0]['id']

        supabase.table('stock_tiendas').insert([
            {'producto_id': prod_id, 'tienda_id': 1, 'stock': stock_t1, 'stock_minimo': minimo_t1},
            {'producto_id': prod_id, 'tienda_id': 2, 'stock': stock_t2, 'stock_minimo': minimo_t2}
        ]).execute()

        auditoria('producto_creado', {'producto_id': prod_id, 'nombre': nombre})
        return jsonify({'mensaje': 'Producto creado', 'id': prod_id}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/producto/<int:prod_id>', methods=['PUT'])
@login_required
@admin_required
def editar_producto(prod_id):
    data = request.get_json()
    try:
        update = {}
        if 'codigo' in data:
            update['codigo'] = (data['codigo'] or '').strip() or None
        if 'nombre' in data:
            update['nombre'] = data['nombre'].strip()
        if 'categoria' in data:
            update['categoria'] = (data['categoria'] or '').strip() or None
        if 'precio' in data:
            update['precio'] = float(data['precio'])

        supabase.table('productos').update(update).eq('id', prod_id).execute()
        auditoria('producto_editado', {'producto_id': prod_id, 'cambios': update})
        return jsonify({'mensaje': 'Producto actualizado'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/stock/entrada', methods=['POST'])
@login_required
@admin_required
def entrada_stock():
    data = request.get_json()
    producto_id = int(data.get('producto_id', 0))
    tienda_id = int(data.get('tienda_id', 0))
    cantidad = int(data.get('cantidad', 0))
    motivo = (data.get('motivo') or 'Compra').strip()

    if cantidad <= 0:
        return jsonify({'error': 'La cantidad debe ser mayor a 0'}), 400
    if tienda_id not in (1, 2):
        return jsonify({'error': 'Tienda inválida'}), 400

    try:
        stock = supabase.table('stock_tiendas').select('*').eq('producto_id', producto_id).eq('tienda_id', tienda_id).execute()
        if not stock.data:
            return jsonify({'error': 'Producto no tiene stock en esa tienda'}), 400

        stock_actual = stock.data[0]['stock']
        nuevo = stock_actual + cantidad

        supabase.table('stock_tiendas').update({'stock': nuevo}).eq('producto_id', producto_id).eq('tienda_id', tienda_id).execute()

        supabase.table('movimientos_inventario').insert({
            'producto_id': producto_id,
            'tienda_id': tienda_id,
            'tipo': 'entrada',
            'cantidad': cantidad,
            'referencia_id': None
        }).execute()

        auditoria('entrada_stock', {'producto_id': producto_id, 'tienda': tienda_id, 'cantidad': cantidad, 'motivo': motivo})
        return jsonify({'mensaje': 'Stock actualizado', 'nuevo_stock': nuevo}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/stock/ajuste', methods=['POST'])
@login_required
@admin_required
def ajuste_stock():
    data = request.get_json()
    producto_id = int(data.get('producto_id', 0))
    tienda_id = int(data.get('tienda_id', 0))
    stock_real = int(data.get('stock_real', 0))
    motivo = (data.get('motivo') or 'Ajuste').strip()

    if tienda_id not in (1, 2):
        return jsonify({'error': 'Tienda inválida'}), 400

    try:
        stock = supabase.table('stock_tiendas').select('stock').eq('producto_id', producto_id).eq('tienda_id', tienda_id).execute()
        if not stock.data:
            return jsonify({'error': 'Producto sin stock en esa tienda'}), 400

        stock_anterior = stock.data[0]['stock']
        diferencia = stock_real - stock_anterior

        supabase.table('stock_tiendas').update({'stock': stock_real}).eq('producto_id', producto_id).eq('tienda_id', tienda_id).execute()

        if diferencia != 0:
            supabase.table('movimientos_inventario').insert({
                'producto_id': producto_id,
                'tienda_id': tienda_id,
                'tipo': 'ajuste',
                'cantidad': diferencia,
                'referencia_id': None
            }).execute()

        auditoria('ajuste_stock', {
            'producto_id': producto_id,
            'tienda': tienda_id,
            'stock_anterior': stock_anterior,
            'stock_nuevo': stock_real,
            'diferencia': diferencia,
            'motivo': motivo
        })

        return jsonify({
            'mensaje': 'Ajuste registrado',
            'stock_anterior': stock_anterior,
            'stock_nuevo': stock_real,
            'diferencia': diferencia
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/producto/<int:prod_id>', methods=['DELETE'])
@login_required
@admin_required
def eliminar_producto(prod_id):
    try:
        supabase.table('productos').delete().eq('id', prod_id).execute()
        auditoria('producto_eliminado', {'producto_id': prod_id})
        return jsonify({'mensaje': 'Producto eliminado'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------- API: DASHBOARD ----------------

@app.route('/api/dashboard')
@login_required
@admin_required
def dashboard():
    hoy = date.today().isoformat()

    try:
        ventas = supabase.table('ventas').select(
            'total, metodo_pago, fecha, usuario, id, tienda_id, tiendas(nombre)'
        ).eq('estado', 'completada').gte('fecha', hoy + 'T00:00:00').order('fecha', desc=True).execute()

        total_ventas = sum(v['total'] for v in ventas.data)
        num_ventas = len(ventas.data)
        efectivo = sum(v['total'] for v in ventas.data if v['metodo_pago'] == 'efectivo')
        yape = sum(v['total'] for v in ventas.data if v['metodo_pago'] in ('yape', 'plin'))

        cajas = supabase.table('caja_sesiones').select('*, tiendas(nombre)').eq('fecha', hoy).execute()

        productos = supabase.table('productos_con_stock').select('nombre, stock, stock_minimo, tienda_id').execute()
        stock_bajo = [p for p in productos.data if p['stock'] <= (p['stock_minimo'] or 5)]

        detalle = supabase.table('venta_detalle').select(
            'cantidad, ventas!inner(fecha, estado)'
        ).eq('ventas.estado', 'completada').gte('ventas.fecha', hoy + 'T00:00:00').execute()
        productos_vendidos = sum(d['cantidad'] for d in detalle.data)

        pendientes = supabase.table('caja_sesiones').select('id').eq('estado_cierre', 'pendiente_verificacion').execute()

        detalle_full = supabase.table('venta_detalle').select(
            'cantidad, productos!inner(nombre), ventas!inner(fecha, estado)'
        ).eq('ventas.estado', 'completada').gte('ventas.fecha', hoy + 'T00:00:00').execute()
        conteo = {}
        for d in detalle_full.data:
            n = d['productos']['nombre']
            conteo[n] = conteo.get(n, 0) + d['cantidad']
        top = sorted(conteo.items(), key=lambda x: -x[1])[:5]

        return jsonify({
            'fecha': hoy,
            'total_ventas': total_ventas,
            'num_ventas': num_ventas,
            'efectivo': efectivo,
            'yape_plin': yape,
            'productos_vendidos': productos_vendidos,
            'cajas': cajas.data,
            'stock_bajo': stock_bajo,
            'top_productos': top,
            'ventas_recientes': ventas.data[:10],
            'cierres_pendientes': len(pendientes.data)
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True)
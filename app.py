import streamlit as st
import pymysql
import pandas as pd
from datetime import datetime
import base64
import qrcode
from io import BytesIO

# Configuración de página
st.set_page_config(page_title="Sistema de Recepción - POD", layout="wide")

# --- CONEXIÓN A TiDB ---
@st.cache_resource
def init_connection():
    return pymysql.connect(
        host=st.secrets["tidb"]["host"],
        port=st.secrets["tidb"]["port"],
        user=st.secrets["tidb"]["user"],
        password=st.secrets["tidb"]["password"],
        database=st.secrets["tidb"]["database"],
        autocommit=True
    )

conn = init_connection()

# --- INICIALIZACIÓN DE ESTADOS ---
if 'step' not in st.session_state:
    st.session_state.step = 1
if 'pod_data' not in st.session_state:
    st.session_state.pod_data = {'pallets': []}
if 'current_pallet' not in st.session_state:
    st.session_state.current_pallet = 1

# --- MENÚ LATERAL ---
menu = st.sidebar.radio("Navegación", ["Nueva Recepción (POD)", "Historial de PODs"])

# ==========================================
# PANTALLA 1: NUEVA RECEPCIÓN
# ==========================================
if menu == "Nueva Recepción (POD)":
    st.title("📦 Generación de POD de Recepción")

    # PASO 1: Datos Generales
    if st.session_state.step == 1:
        st.header("Paso 1: Información del Proveedor")
        provider_options = ["iMile", "J&T", "Forza", "Otro"]
        selected_provider = st.selectbox("Seleccione el proveedor", provider_options)
        
        if selected_provider == "Otro":
            provider = st.text_input("Escriba el nombre del proveedor")
        else:
            provider = selected_provider

        pallets_count = st.number_input("Cantidad total de pallets a recibir", min_value=1, step=1)

        if st.button("Siguiente ➡️"):
            if provider:
                st.session_state.pod_data['provider'] = provider
                st.session_state.pod_data['total_pallets'] = pallets_count
                st.session_state.step = 2
                st.rerun()
            else:
                st.warning("Por favor ingrese el nombre del proveedor.")

    # PASO 2: Revisión por Pallet
    elif st.session_state.step == 2:
        current = st.session_state.current_pallet
        total = st.session_state.pod_data['total_pallets']
        
        st.header(f"Paso 2: Revisión de Pallet {current} de {total}")
        
        box_count = st.number_input(f"Cantidad de cajas en el Pallet {current}", min_value=1, step=1)
        has_damage = st.radio("¿Hay cajas dañadas en este pallet?", ["No", "Sí"])
        
        photos = []
        if has_damage == "Sí":
            st.warning("📸 Por favor, tome fotos de las cajas dañadas antes de cerrar el pallet.")
            uploaded_files = st.file_uploader("Adjuntar fotos", type=['png', 'jpg', 'jpeg'], accept_multiple_files=True)
            for file in uploaded_files:
                # Convertir imagen a Base64
                bytes_data = file.getvalue()
                base64_img = base64.b64encode(bytes_data).decode('utf-8')
                photos.append(base64_img)

        if st.button("Guardar Pallet y Continuar 📦"):
            # Guardar datos del pallet en sesión
            st.session_state.pod_data['pallets'].append({
                'pallet_number': current,
                'box_count': box_count,
                'has_damage': True if has_damage == "Sí" else False,
                'photos': photos
            })
            
            if current < total:
                st.session_state.current_pallet += 1
            else:
                st.session_state.step = 3
            st.rerun()

    # PASO 3: Firmas y Generación
    elif st.session_state.step == 3:
        st.header("Paso 3: Confirmación y Firmas")
        
        st.write("### Resumen de Recepción")
        df_summary = pd.DataFrame(st.session_state.pod_data['pallets'])
        df_summary['Cantidad de Fotos'] = df_summary['photos'].apply(len)
        st.dataframe(df_summary[['pallet_number', 'box_count', 'has_damage', 'Cantidad de Fotos']])
        
        col1, col2 = st.columns(2)
        with col1:
            receiver_name = st.text_input("Nombre de quien recibe (Tú)")
            # Nota: Para firmas reales puedes usar 'streamlit-drawable-canvas'
            receiver_sig = st.text_input("Firma de quien recibe (Escribir iniciales por ahora)") 
        with col2:
            deliverer_name = st.text_input("Nombre de quien entrega (Proveedor)")
            deliverer_sig = st.text_input("Firma de quien entrega")

        if st.button("✅ Generar y Guardar POD"):
            # 1. Insertar en TiDB (Tabla pods)
            with conn.cursor() as cursor:
                sql_pod = "INSERT INTO pods (provider_name, total_pallets, receiver_name, deliverer_name, receiver_signature, deliverer_signature) VALUES (%s, %s, %s, %s, %s, %s)"
                cursor.execute(sql_pod, (st.session_state.pod_data['provider'], st.session_state.pod_data['total_pallets'], receiver_name, deliverer_name, receiver_sig, deliverer_sig))
                pod_id = cursor.lastrowid
                
                # 2. Insertar Pallets y Fotos
                for p in st.session_state.pod_data['pallets']:
                    sql_pallet = "INSERT INTO pallets (pod_id, pallet_number, box_count, has_damage) VALUES (%s, %s, %s, %s)"
                    cursor.execute(sql_pallet, (pod_id, p['pallet_number'], p['box_count'], p['has_damage']))
                    pallet_id = cursor.lastrowid
                    
                    for photo_b64 in p['photos']:
                        sql_photo = "INSERT INTO damaged_photos (pallet_id, image_data) VALUES (%s, %s)"
                        cursor.execute(sql_photo, (pallet_id, photo_b64))
            
            st.success(f"¡POD #{pod_id} generada y guardada exitosamente en TiDB!")
            st.session_state.step = 1 # Reiniciar wizard
            st.session_state.pod_data = {'pallets': []}
            st.session_state.current_pallet = 1

# ==========================================
# PANTALLA 2: HISTORIAL
# ==========================================
elif menu == "Historial de PODs":
    st.title("📜 Historial de Recepciones")
    
    # Obtener params de URL para el código QR
    query_params = st.query_params
    
    # Si viene de un escaneo de QR (URL termina en ?pod_id=X)
    if "pod_id" in query_params:
        target_pod = query_params["pod_id"]
        st.subheader(f"Visor de Evidencias - POD #{target_pod}")
        # Aquí harías un SELECT a TiDB uniendo las fotos del pod_id
        st.info("Aquí se muestran las fotos extraídas de la base de datos para este POD.")
        if st.button("Volver al Historial General"):
            st.query_params.clear()
            st.rerun()
            
    else:
        # Mostrar tabla general
        df_pods = pd.read_sql("SELECT id as POD_ID, provider_name as Proveedor, total_pallets as Pallets, created_at as Fecha FROM pods ORDER BY id DESC", conn)
        st.dataframe(df_pods)
        
        # Seleccionar uno para ver detalles / Imprimir PDF
        selected_pod = st.selectbox("Seleccione un POD para ver detalles o imprimir", df_pods['POD_ID'])
        if selected_pod:
            # Aquí podrías usar la librería FPDF para generar un archivo .pdf descargable
            st.markdown(f"**Leyenda Legal:** *Se reciben cajas cerradas no se han contado la cantidad de paquetes que se están recibiendo, por lo que luego de la revisión se contarán las piezas.*")
            
            # Generar QR dinámico
            url_qr = f"https://tu-app-en-streamlit.app/?pod_id={selected_pod}"
            qr = qrcode.make(url_qr)
            # Mostrar QR en pantalla
            st.image(qr.get_image(), caption=f"QR de Evidencias POD #{selected_pod}", width=150)

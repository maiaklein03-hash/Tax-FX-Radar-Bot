# Prospectos Tax & FX Bot

Bot de Telegram que revisa diariamente fuentes oficiales y avisa únicamente
cuando una norma puede exigir revisar los capítulos **Controles de Cambio** o
**Tratamiento Impositivo** de prospectos de:

- obligaciones negociables corporativas;
- títulos de deuda provinciales.

## Fuentes

- BCRA: Comunicaciones del buscador oficial (núcleo del análisis cambiario).
- CNV: Resoluciones Generales (oferta pública, colocación, integración, canjes,
  valores negociables y condiciones relacionadas con beneficios fiscales).
- Boletín Oficial: módulo tributario federal indispensable para leyes, decretos y
  normas de ARCA. No agrega noticias ni normas generales sin impacto en modelos.

## Instalación

1. Crear un bot nuevo con `@BotFather`, abrirlo y presionar **START**.
2. Crear un repositorio privado nuevo en GitHub.
3. Subir el contenido de esta carpeta, incluida `.github/workflows/tax-fx.yml`.
4. En `Settings > Secrets and variables > Actions`, crear:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `GEMINI_API_KEY`
5. En `Settings > Actions > General > Workflow permissions`, seleccionar
   **Read and write permissions** y guardar.
6. Ejecutar manualmente una vez con `modo_prueba: false`. Esa primera ejecución
   guarda la foto inicial y no envía normas viejas.

## Prueba

En `Actions > Revisar Tax y Cambiario para Prospectos > Run workflow`, elegir
`modo_prueba: true`. Evaluará una muestra actual por fuente, sin modificar el
historial, y enviará siempre un mensaje final de control.

## Automatización y consumo

- Corre de lunes a viernes a las 09:07 de Buenos Aires.
- No envía “sin novedades” en las ejecuciones normales.
- Gemini solo se usa para publicaciones nuevas que superan el filtro temático.
- Cada norma se registra en `estado.json`, para no repetirla.

## Alcance tributario provincial

El bot detecta normativa federal. IIBB, Sellos, recaudaciones bancarias e ITGB
dependen de la provincia concreta; para vigilarlos hace falta indicar qué
jurisdicciones provinciales deben incorporarse como fuentes adicionales.

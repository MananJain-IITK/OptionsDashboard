from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('api/strikes/', views.get_strikes, name='get_strikes'),
    path('api/option-data/', views.get_option_data, name='get_option_data'),
    path('analysis/', views.analysis_hub, name='analysis_hub'),
    path('greeks/', views.greeks_calculator, name='greeks_calculator'),
    path('iv-smile/', views.iv_smile, name='iv_smile'),
    path('api/greeks-data/', views.get_greeks_data, name='get_greeks_data'),
    path('api/iv-smile-data/', views.get_iv_smile_data, name='get_iv_smile_data'),
    path('api/iv-surface-data/', views.get_iv_surface_data, name='get_iv_surface_data'),
    path('api/diagnostics/', views.diagnostics, name='diagnostics'),
    path('api/risk-free-rate/', views.get_rfr, name='get_rfr'),
]

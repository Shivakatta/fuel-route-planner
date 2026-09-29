from django.db import models


class FuelStation(models.Model):
	station_id = models.PositiveIntegerField(primary_key=True)
	name = models.CharField(max_length=200)
	address = models.CharField(max_length=255, blank=True)
	city = models.CharField(max_length=100)
	state = models.CharField(max_length=2, db_index=True)
	rack_id = models.PositiveIntegerField(null=True, blank=True)
	price = models.DecimalField(max_digits=7, decimal_places=3)
	price_min = models.DecimalField(max_digits=7, decimal_places=3, null=True, blank=True)
	price_max = models.DecimalField(max_digits=7, decimal_places=3, null=True, blank=True)
	source_rows = models.PositiveIntegerField(default=1)
	price_outlier = models.BooleanField(default=False)
	latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
	longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
	location_source = models.CharField(max_length=32, blank=True)

	class Meta:
		indexes = [
			models.Index(fields=("latitude", "longitude"), name="station_coordinates_idx"),
			models.Index(fields=("state", "city"), name="station_locality_idx"),
		]

	def __str__(self):
		return f"{self.name} - {self.city}, {self.state}"

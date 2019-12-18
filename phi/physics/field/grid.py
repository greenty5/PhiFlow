import numpy as np
import six

from phi import math, struct
from phi.geom import AABox
from phi.geom.geometry import assert_same_rank
from phi.struct.functions import mappable
from phi.struct.tensorop import collapse, collapsed_gather_nd

from .field import Field, propagate_flags_children
from .flag import SAMPLE_POINTS


def _crop_for_interpolation(data, offset_float, window_resolution):
    offset = math.to_int(offset_float)
    slices = [slice(o, o+res+1) for o, res in zip(offset, window_resolution)]
    data = data[[slice(None)] + slices + [slice(None)]]
    return data


@struct.definition()
class CenteredGrid(Field):

    def __init__(self, data, box=None, extrapolation='boundary', name=None, **kwargs):
        Field.__init__(self, **struct.kwargs(locals()))
        self._sample_points = None

    @property
    def resolution(self):
        return math.as_tensor(math.staticshape(self.data)[1:-1])

    @struct.constant(dependencies=Field.data)
    def box(self, box):
        return AABox.to_box(box, resolution_hint=self.resolution)

    @property
    def dx(self):
        return self.box.size / self.resolution

    @property
    def rank(self):
        return math.spatial_rank(self.data)

    @property
    def shape(self):
        with struct.unsafe():
            return self.copied_with(age=(), data=(self._batch_size,) + math.staticshape(self.data)[1:])

    @struct.constant(default='boundary')
    def extrapolation(self, extrapolation):
        if extrapolation is None:
            return 'boundary'
        assert extrapolation in ('periodic', 'constant', 'boundary') or isinstance(extrapolation, (tuple, list)), extrapolation
        return collapse(extrapolation)

    @struct.constant(default='linear')
    def interpolation(self, interpolation):
        assert interpolation == 'linear'
        return interpolation

    def sample_at(self, points, collapse_dimensions=True):
        if not isinstance(self.extrapolation, six.string_types):
            return self._padded_resample(points)
        local_points = self.box.global_to_local(points)
        local_points = local_points * math.to_float(self.resolution) - 0.5
        if self.extrapolation == 'periodic':
            data = math.pad(self.data, [[0,0]]+[[0,1]]*self.rank+[[0,0]], mode='wrap')
            local_points = local_points % math.to_float(math.staticshape(self.data)[1:-1])
            resampled = math.resample(data, local_points, interpolation=self.interpolation)
        else:
            boundary = 'replicate' if self.extrapolation == 'boundary' else 'zero'
            resampled = math.resample(self.data, local_points, boundary=boundary, interpolation=self.interpolation)
        return resampled

    def at(self, other_field, collapse_dimensions=True, force_optimization=False, return_self_if_compatible=False):
        if self.compatible(other_field):  # and return_self_if_compatible: not applicable for fields with Points
            return self
        if isinstance(other_field, CenteredGrid) and np.allclose(self.dx, other_field.dx):
            paddings = _required_paddings_transposed(self.box, self.dx, other_field.box)
            if math.sum(paddings) == 0:
                origin_in_local = self.box.global_to_local(other_field.box.lower) * self.resolution
                data = _crop_for_interpolation(self.data, origin_in_local, other_field.resolution)
                dimensions = self.resolution != other_field.resolution
                dimensions = [d for d in math.spatial_dimensions(data) if dimensions[d-1]]
                data = math.interpolate_linear(data, origin_in_local % 1.0, dimensions)
                return CenteredGrid(data, other_field.box, name=self.name, batch_size=self._batch_size)
            elif math.sum(paddings) < 16:
                padded = self.padded(np.transpose(paddings).tolist())
                return padded.at(other_field, collapse_dimensions, force_optimization)
        return Field.at(self, other_field, force_optimization=force_optimization)

    @property
    def component_count(self):
        return self.data.shape[-1]

    def unstack(self):
        flags = propagate_flags_children(self.flags, self.rank, 1)
        return [
            CenteredGrid(component, self.box, '%s[...,%d]' % (self.name, i), flags=flags, batch_size=self._batch_size)
            for i, component in enumerate(math.unstack(self.data, -1))]

    @property
    def points(self):
        if SAMPLE_POINTS in self.flags:
            return self
        if self._sample_points is None:
            self._sample_points = CenteredGrid.getpoints(self.box, self.resolution)
        return self._sample_points

    def compatible(self, other_field):
        if not other_field.has_points:
            return True
        if isinstance(other_field, CenteredGrid):
            if self.box != other_field.box:
                return False
            if self.rank != other_field.rank:
                return False
            for r1, r2 in zip(self.resolution, other_field.resolution):
                if r1 != r2 and r2 != 1 and r1 != 1:
                    return False
            return True
        else:
            return False

    def __repr__(self):
        try:
            return 'Grid[%s(%d), size=%s]' % ('x'.join([str(r) for r in self.resolution]), self.component_count, self.box.size)
        except:
            return 'Grid[invalid]'

    def padded(self, widths):
        extrapolation = self.extrapolation if isinstance(self.extrapolation, six.string_types) else ['constant'] + list(self.extrapolation) + ['constant']
        data = math.pad(self.data, [[0, 0]]+widths+[[0, 0]], _pad_mode(extrapolation))
        w_lower, w_upper = np.transpose(widths)
        box = AABox(self.box.lower - w_lower * self.dx, self.box.upper + w_upper * self.dx)
        return CenteredGrid(data, box, extrapolation=self.extrapolation, name=self.name, batch_size=self._batch_size)

    def axis_padded(self, axis, lower, upper):
        widths = [[lower, upper] if ax == axis else [0,0] for ax in range(self.rank)]
        return self.padded(widths)

    @staticmethod
    def getpoints(box, resolution):
        idx_zyx = np.meshgrid(*[np.linspace(0.5 / dim, 1 - 0.5 / dim, dim) for dim in resolution], indexing="ij")
        local_coords = math.expand_dims(math.stack(idx_zyx, axis=-1), 0).astype(np.float32)
        points = box.local_to_global(local_coords)
        return CenteredGrid(points, box, name='grid_centers(%s, %s)' % (box, resolution), flags=[SAMPLE_POINTS])

    def laplace(self, physical_units=True):
        if not physical_units:
            return math.laplace(self.data, padding=_pad_mode(self.extrapolation))
        else:
            if not np.allclose(self.dx, np.mean(self.dx)):
                raise NotImplementedError('Only cubic cells supported.')
            laplace = math.laplace(self.data, padding=_pad_mode(self.extrapolation))
            return laplace / self.dx[0] ** 2

    def normalized(self, total, epsilon=1e-5):
        if isinstance(total, CenteredGrid):
            total = total.data
        normalize_data = math.normalize_to(self.data, total, epsilon)
        return self.with_data(normalize_data)

    def _padded_resample(self, points):
        data = self.padded([[1,1]] * self.rank).data
        local_points = self.box.global_to_local(points)
        local_points = local_points * math.to_float(self.resolution) + 0.5  # depends on amount of padding
        resampled = math.resample(data, local_points, boundary='replicate', interpolation=self.interpolation)
        return resampled


def _required_paddings_transposed(box, dx, target):
    lower = math.to_int(math.ceil(math.maximum(0, box.lower - target.lower) / dx))
    upper = math.to_int(math.ceil(math.maximum(0, target.upper - box.upper) / dx))
    return [lower, upper]


@mappable()
def _pad_mode(extrapolation):
    if extrapolation == 'periodic':
        return 'wrap'
    elif extrapolation == 'boundary':
        return 'symmetric'
    else:
        return extrapolation

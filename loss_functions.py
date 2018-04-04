import lpips.dist_model as dm
import torch.nn as nn
import torch
from numbers import Number
import utils.image_utils as img_utils

""" Loss function building blocks """

##############################################################################
#                                                                            #
#                        LOSS FUNCTION WRAPPER                               #
#                                                                            #
##############################################################################

class RegularizedLoss(object):
    """ Wrapper for multiple PartialLoss objects where we combine with
        regularization constants """
    def __init__(self, losses, scalars):
        """
        ARGS:
            losses : dict - dictionary of partialLoss objects, each is keyed
                            with a nice identifying name
            scalars : dict - dictionary of scalars, each is keyed with the
                             same identifying name as is in self.losses
        """

        assert sorted(losses.keys()) == sorted(scalars.keys())

        self.losses = losses
        self.scalars = scalars

    def forward(self, examples, labels, *args, **kwargs):

        output = None
        for k in self.losses:
            loss = self.losses[k]
            scalar = self.scalars[k]

            loss_val = loss.forward(examples, labels, *args, **kwargs)

            # assert scalar is either a...
            assert (isinstance(scalar, float) or # number
                    scalar.numel() == 1 or # tf wrapping of a number
                    scalar.shape == loss_val.shape) # same as the shape of loss

            if output is None:
                output = loss_val * scalar
            else:
                output = output + loss_val * scalar

        return output


    def setup_attack_batch(self, fix_im):
        """ Setup before calling loss on a new minibatch. Ensures the correct
            fix_im for reference regularizers and that all grads are zeroed
        ARGS:
            fix_im: Variable (NxCxHxW) - Ground images for this minibatch
                    SHOULD BE IN [0.0, 1.0] RANGE
        """
        for loss in self.losses.itervalues():
            if isinstance(loss, ReferenceRegularizer):
                loss.setup_attack_batch(fix_im)
            else:
                loss.zero_grad()


    def cleanup_attack_batch(self):
        """ Does some cleanup stuff after we finish on a minibatch:
        - clears the fixed images for ReferenceRegularizers
        - zeros grads
        - clears example-based scalars (i.e. scalars that depend on which
          example we're using)
        """
        for loss in self.losses.itervalues():
            if isinstance(loss, ReferenceRegularizer):
                loss.cleanup_attack_batch()
            else:
                loss.zero_grad()

        for key, scalar in self.scalars.items():
            if not isinstance(scalar, Number):
                self.scalars[key] = None


    def zero_grad(self):
        for loss in self.losses.itervalues():
            loss.zero_grad() # probably zeros the same net more than once...



class PartialLoss(object):
    """ Partially applied loss object. Has forward and zero_grad methods """
    def __init__(self):
        self.nets = []

    def zero_grad(self):
        for net in self.nets:
            net.zero_grad()


##############################################################################
#                                                                            #
#                                  LOSS FUNCTIONS                            #
#                                                                            #
##############################################################################

##############################################################################
#                                   Standard XEntropy Loss                   #
##############################################################################

class PartialXentropy(PartialLoss):
    def __init__(self, classifier, normalizer=None):
        super(PartialXentropy, self).__init__()
        self.classifier = classifier
        self.normalizer = normalizer
        self.nets.append(self.classifier)

    def forward(self, examples, labels, *args, **kwargs):
        """ Returns XEntropy loss
        ARGS:
            examples: Variable (NxCxHxW) - should be same shape as
                      ctx.fix_im, is the examples we define loss for.
                      SHOULD BE IN [0.0, 1.0] RANGE
            labels: Variable (longTensor of length N) - true classification
                    output for fix_im/examples
        RETURNS:
            scalar loss variable
        """

        if self.normalizer is not None:
            normed_examples = self.normalizer.forward(examples)
        else:
            normed_examples = examples

        criterion = nn.CrossEntropyLoss()
        return criterion(self.classifier.forward(normed_examples), labels)

##############################################################################
#                           Carlini Wagner loss functions                    #
##############################################################################

class CWLossF6(PartialLoss):
    def __init__(self, classifier, normalizer=None, kappa=0.0):
        super(CWLossF6, self).__init__()
        self.classifier = classifier
        self.normalizer = normalizer
        self.nets.append(self.classifier)
        self.kappa = kappa


    def forward(self, examples, labels, *args, **kwargs):
        classifier_in = self.normalizer.forward(examples)
        classifier_out = self.classifier.forward(classifier_in)

        # get target logits
        target_logits = torch.gather(classifier_out, 1, labels.view(-1, 1))

        # get largest non-target logits
        max_2_logits, argmax_2_logits = torch.topk(classifier_out, 2, dim=1)
        top_max, second_max = max_2_logits.chunk(2, dim=1)
        top_argmax, _ = argmax_2_logits.chunk(2, dim=1)
        targets_eq_max = top_argmax.squeeze().eq(labels).float().view(-1, 1)
        targets_ne_max = top_argmax.squeeze().ne(labels).float().view(-1, 1)
        max_other = targets_eq_max * second_max + targets_ne_max * top_max


        if kwargs.get('targeted', False):
            # in targeted case, want to make target most likely
            f6 = torch.clamp(max_other - target_logits, min=-1 * self.kappa)
        else:
            # in NONtargeted case, want to make NONtarget most likely
            f6 = torch.clamp(target_logits - max_other, min=-1 * self.kappa)

        return f6





##############################################################################
#                                                                            #
#                               REFERENCE REGULARIZERS                       #
#                                                                            #
##############################################################################
""" Regularization terms that refer back to a set of 'fixed images', or the
    original images.
    example: L2 regularization which computes L2dist between a perturbed image
             and the FIXED ORIGINAL IMAGE
"""

class ReferenceRegularizer(PartialLoss):
    def __init__(self, fix_im):
        super(ReferenceRegularizer, self).__init__()

    def setup_attack_batch(self, fix_im):
        """ Setup function to ensure fixed images are set
            has been made; also zeros grads
        ARGS:
            fix_im: Variable (NxCxHxW) - Ground images for this minibatch
                    SHOULD BE IN [0.0, 1.0] RANGE
        """
        self.fix_im = fix_im
        self.zero_grad()


    def cleanup_attack_batch(self):
        """ Cleanup function to clear the fixed images after an attack batch
            has been made; also zeros grads
        """
        self.fix_im = None
        self.zero_grad()



#############################################################################
#                               L2 REGULARIZATION                           #
#############################################################################

class L2Regularization(ReferenceRegularizer):

    def __init__(self, fix_im, **kwargs):
        super(L2Regularization, self).__init__(fix_im)

    def forward(self, examples, *args, **kwargs):
        return img_utils.nchw_l2(examples, self.fix_im,
                                 squared=True).view(-1, 1)

#############################################################################
#                         LPIPS PERCEPTUAL REGULARIZATION                   #
#############################################################################

class LpipsRegularization(ReferenceRegularizer):

    def __init__(self, fix_im, **kwargs):
        super(LpipsRegularization, self).__init__(fix_im)

        use_gpu = kwargs.get('use_gpu', False)
        dist_model = dm.DistModel()
        dist_model.initialize(model='net-lin',net='alex',use_gpu=use_gpu)
        self.dist_model = dist_model
        self.nets.append(self.dist_model)

    def forward(self, examples, *args, **kwargs):
        xform = lambda im: im * 2.0 - 1.0
        perceptual_loss = self.dist_model.forward_var(2 * examples - 1.,
                                                      2 * self.fix_im - 1.)
        return perceptual_loss




